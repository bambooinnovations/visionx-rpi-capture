from __future__ import annotations

import io
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import structlog


class CameraMode(str, Enum):
    STREAM = "stream"
    CAPTURE = "capture"
    HARDWARE_TRIGGER = "hardware_trigger"

try:
    import cv2
    import numpy as np
    from PIL import Image as PilImage
    import mvsdk

    _MVSDK_AVAILABLE = True
except Exception:
    # mvsdk.py loads the vendor SDK's native library at import time (via
    # ctypes windll/cdll) and raises whatever ctypes throws for a missing
    # library — AttributeError on Windows, OSError on Linux — not just
    # ImportError. Catch broadly so a dev machine without the SDK installed
    # can still import this module (e.g. for "mock" camera.type).
    _MVSDK_AVAILABLE = False

# Guards so CameraSdkInit / CameraSetDataDirectory are only called once
# regardless of how many MindVisionCamera instances are opened.
_sdk_initialized = False

# Cached device list from the first CameraEnumerateDevice call.
# Re-enumerating after some cameras are already initialized can return a
# different ordering or omit initialized devices, causing camera_index to
# map to the wrong physical camera or fail with "not found".
_dev_list_cache: list | None = None

import config
from camera.base import BaseCamera
from metrics import CaptureMetrics

logger = structlog.get_logger()


def _parse_exposure_config(config_file: Path) -> tuple[int, int, float]:
    """Parse ae_enable, ae_target, and exp_time from a MindVision .config file.

    Returns (ae_state, ae_target, exp_time_us) with safe defaults if parsing fails.
    """
    ae_state = 1
    ae_target = 100
    exp_time = 30000.0
    try:
        import re
        text = config_file.read_text(errors="replace")
        m = re.search(r'\bae_enable\s*=\s*(true|false)', text)
        if m:
            ae_state = 1 if m.group(1) == "true" else 0
        m = re.search(r'\bae_target\s*=\s*(\d+)', text)
        if m:
            ae_target = int(m.group(1))
        m = re.search(r'\bexp_time\s*=\s*([0-9.eE+\-]+)', text)
        if m:
            exp_time = float(m.group(1))
    except Exception:
        pass
    return ae_state, ae_target, exp_time


def read_hw_settings(h: int, cap) -> dict:
    """Read every tunable SDK parameter (and its range) from the camera hardware."""
    import mvsdk
    s = {}

    try: s["ae_enabled"] = bool(mvsdk.CameraGetAeState(h))
    except Exception: s["ae_enabled"] = True

    try: s["exposure_us"] = mvsdk.CameraGetExposureTime(h)
    except Exception: s["exposure_us"] = 30000.0

    try:
        exp_min, exp_max, _ = mvsdk.CameraGetExposureTimeRange(h)
        s["exposure_min_us"] = exp_min
        s["exposure_max_us"] = exp_max
    except Exception:
        s["exposure_min_us"] = 26.0
        s["exposure_max_us"] = 1_000_000.0

    try: s["ae_target"] = mvsdk.CameraGetAeTarget(h)
    except Exception: s["ae_target"] = 100

    # Analog gain is in raw SDK units; multiplier = raw * analog_gain_step.
    try:
        s["analog_gain"] = mvsdk.CameraGetAnalogGain(h)
        s["analog_gain_min"] = cap.sExposeDesc.uiAnalogGainMin if cap else 16
        s["analog_gain_max"] = cap.sExposeDesc.uiAnalogGainMax if cap else 128
        s["analog_gain_step"] = float(cap.sExposeDesc.fAnalogGainStep) if cap else 0.125
    except Exception:
        s.update(analog_gain=16, analog_gain_min=16, analog_gain_max=128, analog_gain_step=0.125)

    try:
        gain_lo, gain_hi = mvsdk.CameraGetAeAnalogGainRange(h)
        s["auto_gain"] = bool(s["ae_enabled"]) and gain_lo != gain_hi
    except Exception:
        s["auto_gain"] = bool(s["ae_enabled"])

    try:
        r, g, b = mvsdk.CameraGetGain(h)
        s.update(r_gain=r, g_gain=g, b_gain=b)
        if cap:
            s.update(
                r_gain_min=cap.sRgbGainRange.iRGainMin, r_gain_max=cap.sRgbGainRange.iRGainMax,
                g_gain_min=cap.sRgbGainRange.iGGainMin, g_gain_max=cap.sRgbGainRange.iGGainMax,
                b_gain_min=cap.sRgbGainRange.iBGainMin, b_gain_max=cap.sRgbGainRange.iBGainMax,
            )
        else:
            s.update(r_gain_min=0, r_gain_max=400, g_gain_min=0, g_gain_max=400,
                     b_gain_min=0, b_gain_max=400)
    except Exception:
        s.update(r_gain=100, g_gain=100, b_gain=100,
                 r_gain_min=0, r_gain_max=400, g_gain_min=0, g_gain_max=400,
                 b_gain_min=0, b_gain_max=400)

    try:
        s["sharpness"] = mvsdk.CameraGetSharpness(h)
        s["sharpness_min"] = cap.sSharpnessRange.iMin if cap else 0
        s["sharpness_max"] = cap.sSharpnessRange.iMax if cap else 100
    except Exception:
        s.update(sharpness=0, sharpness_min=0, sharpness_max=100)

    try:
        s["gamma"] = mvsdk.CameraGetGamma(h)
        s["gamma_min"] = cap.sGammaRange.iMin if cap else 0
        s["gamma_max"] = cap.sGammaRange.iMax if cap else 250
    except Exception:
        s.update(gamma=100, gamma_min=0, gamma_max=250)

    try:
        s["contrast"] = mvsdk.CameraGetContrast(h)
        s["contrast_min"] = cap.sContrastRange.iMin if cap else 0
        s["contrast_max"] = cap.sContrastRange.iMax if cap else 200
    except Exception:
        s.update(contrast=100, contrast_min=0, contrast_max=200)

    try:
        s["saturation"] = mvsdk.CameraGetSaturation(h)
        s["saturation_min"] = cap.sSaturationRange.iMin if cap else 0
        s["saturation_max"] = cap.sSaturationRange.iMax if cap else 200
    except Exception:
        s.update(saturation=100, saturation_min=0, saturation_max=200)

    try: s["noise_filter"] = bool(mvsdk.CameraGetNoiseFilterState(h))
    except Exception: s["noise_filter"] = False

    try: s["correct_dead_pixel"] = bool(mvsdk.CameraGetCorrectDeadPixel(h))
    except Exception: s["correct_dead_pixel"] = False

    try: s["inverse"] = bool(mvsdk.CameraGetInverse(h))
    except Exception: s["inverse"] = False

    try: s["anti_flick"] = bool(mvsdk.CameraGetAntiFlick(h))
    except Exception: s["anti_flick"] = False

    try: s["light_frequency"] = mvsdk.CameraGetLightFrequency(h)
    except Exception: s["light_frequency"] = 0

    try:
        s["frame_speed"] = mvsdk.CameraGetFrameSpeed(h)
        s["frame_speed_max"] = max(0, (cap.iFrameSpeedDesc - 1) if cap else 2)
    except Exception:
        s.update(frame_speed=0, frame_speed_max=2)

    try:
        s["rotation"] = mvsdk.CameraGetRotate(h)
        s["h_mirror"] = bool(mvsdk.CameraGetMirror(h, 0))
        s["v_mirror"] = bool(mvsdk.CameraGetMirror(h, 1))
    except Exception:
        s.update(rotation=0, h_mirror=False, v_mirror=False)

    # mono_sensor is a hardware capability (read-only, informs the UI whether
    # the mono_enabled toggle is meaningful at all); mono_enabled is the
    # user-controllable ISP color->mono conversion.
    s["mono_sensor"] = bool(cap.sIspCapacity.bMonoSensor) if cap else False
    try: s["mono_enabled"] = bool(mvsdk.CameraGetMonochrome(h))
    except Exception: s["mono_enabled"] = False

    return s


def apply_settings(
    h: int, body: dict, cam: "MindVisionCamera | None" = None,
) -> tuple[list[str], dict[str, str]]:
    """Apply body fields to camera hardware without saving. Returns (applied, errors).

    Pass `cam` so exposure/AE edits update its capture profile (which a live
    stream may be overriding with auto-exposure) instead of poking the hardware.
    """
    import mvsdk
    applied: list[str] = []
    errors: dict[str, str] = {}

    exposure_keys = [
        k for k in ("ae_enabled", "exposure_us", "ae_target", "auto_gain", "analog_gain")
        if k in body
    ]
    if cam is not None and exposure_keys:
        try:
            cam.set_capture_exposure(
                ae_enabled=body.get("ae_enabled"),
                ae_target=body.get("ae_target"),
                exposure_us=body.get("exposure_us"),
                auto_gain=body.get("auto_gain"),
                analog_gain=body.get("analog_gain"),
            )
            applied.extend(exposure_keys)
        except Exception as exc:
            errors["exposure"] = str(exc)
        body = {k: v for k, v in body.items() if k not in exposure_keys}

    if "ae_enabled" in body:
        try:
            mvsdk.CameraSetAeState(h, 1 if body["ae_enabled"] else 0)
            applied.append("ae_enabled")
        except Exception as exc:
            errors["ae_enabled"] = str(exc)

    # Skip manual exposure when AE is being enabled — setting exposure time
    # while AE is on can cause some SDK builds to silently disable AE.
    ae_on = body.get("ae_enabled", None)
    if "exposure_us" in body and ae_on is not True:
        try:
            mvsdk.CameraSetExposureTime(h, float(body["exposure_us"]))
            applied.append("exposure_us")
        except Exception as exc:
            errors["exposure_us"] = str(exc)

    if "ae_target" in body:
        try:
            mvsdk.CameraSetAeTarget(h, int(body["ae_target"]))
            applied.append("ae_target")
        except Exception as exc:
            errors["ae_target"] = str(exc)

    if "analog_gain" in body:
        try:
            mvsdk.CameraSetAnalogGain(h, int(body["analog_gain"]))
            applied.append("analog_gain")
        except Exception as exc:
            errors["analog_gain"] = str(exc)

    rgb_keys = ("r_gain", "g_gain", "b_gain")
    if any(k in body for k in rgb_keys):
        try:
            r, g, b = mvsdk.CameraGetGain(h)
            mvsdk.CameraSetGain(
                h,
                int(body.get("r_gain", r)),
                int(body.get("g_gain", g)),
                int(body.get("b_gain", b)),
            )
            applied.extend(k for k in rgb_keys if k in body)
        except Exception as exc:
            errors["rgb_gain"] = str(exc)

    if "sharpness" in body:
        try:
            mvsdk.CameraSetSharpness(h, int(body["sharpness"]))
            applied.append("sharpness")
        except Exception as exc:
            errors["sharpness"] = str(exc)

    if "gamma" in body:
        try:
            mvsdk.CameraSetGamma(h, int(body["gamma"]))
            applied.append("gamma")
        except Exception as exc:
            errors["gamma"] = str(exc)

    if "rotation" in body:
        try:
            rot = int(body["rotation"])
            if rot not in (0, 1, 2, 3):
                raise ValueError("must be 0–3")
            mvsdk.CameraSetRotate(h, rot)
            applied.append("rotation")
        except Exception as exc:
            errors["rotation"] = str(exc)

    if "h_mirror" in body:
        try:
            mvsdk.CameraSetMirror(h, 0, int(bool(body["h_mirror"])))
            applied.append("h_mirror")
        except Exception as exc:
            errors["h_mirror"] = str(exc)

    if "v_mirror" in body:
        try:
            mvsdk.CameraSetMirror(h, 1, int(bool(body["v_mirror"])))
            applied.append("v_mirror")
        except Exception as exc:
            errors["v_mirror"] = str(exc)

    if "mono_enabled" in body:
        try:
            mvsdk.CameraSetMonochrome(h, 1 if body["mono_enabled"] else 0)
            applied.append("mono_enabled")
        except Exception as exc:
            errors["mono_enabled"] = str(exc)

    if "contrast" in body:
        try:
            mvsdk.CameraSetContrast(h, int(body["contrast"]))
            applied.append("contrast")
        except Exception as exc:
            errors["contrast"] = str(exc)

    if "saturation" in body:
        try:
            mvsdk.CameraSetSaturation(h, int(body["saturation"]))
            applied.append("saturation")
        except Exception as exc:
            errors["saturation"] = str(exc)

    if "noise_filter" in body:
        try:
            mvsdk.CameraSetNoiseFilter(h, bool(body["noise_filter"]))
            applied.append("noise_filter")
        except Exception as exc:
            errors["noise_filter"] = str(exc)

    if "correct_dead_pixel" in body:
        try:
            mvsdk.CameraSetCorrectDeadPixel(h, bool(body["correct_dead_pixel"]))
            applied.append("correct_dead_pixel")
        except Exception as exc:
            errors["correct_dead_pixel"] = str(exc)

    if "inverse" in body:
        try:
            mvsdk.CameraSetInverse(h, bool(body["inverse"]))
            applied.append("inverse")
        except Exception as exc:
            errors["inverse"] = str(exc)

    if "anti_flick" in body:
        try:
            mvsdk.CameraSetAntiFlick(h, bool(body["anti_flick"]))
            applied.append("anti_flick")
        except Exception as exc:
            errors["anti_flick"] = str(exc)

    if "light_frequency" in body:
        try:
            mvsdk.CameraSetLightFrequency(h, int(body["light_frequency"]))
            applied.append("light_frequency")
        except Exception as exc:
            errors["light_frequency"] = str(exc)

    if "frame_speed" in body:
        try:
            mvsdk.CameraSetFrameSpeed(h, int(body["frame_speed"]))
            applied.append("frame_speed")
        except Exception as exc:
            errors["frame_speed"] = str(exc)

    return applied, errors


# Everything the settings page edits. A "production" set of these values is
# what real captures use; the page edits a draft on the live hardware.
SETTING_KEYS: tuple[str, ...] = (
    "ae_enabled", "ae_target", "exposure_us", "auto_gain", "analog_gain",
    "r_gain", "g_gain", "b_gain", "sharpness", "gamma", "contrast", "saturation",
    "noise_filter", "correct_dead_pixel", "inverse", "anti_flick",
    "light_frequency", "frame_speed", "rotation", "h_mirror", "v_mirror",
    "mono_enabled",
)


def _same_setting(key: str, a, b) -> bool:
    if key == "exposure_us":
        return abs(float(a) - float(b)) < 0.5
    return a == b


class MindVisionCamera(BaseCamera):
    def __init__(self, camera_index: int = 0) -> None:
        self._camera_index = camera_index
        self._project_root = Path(__file__).parent.parent
        self._h_camera: int | None = None
        self._frame_buffer: int = 0  # aligned C buffer; 0 means not yet allocated
        self._mono: bool = False
        self._cap = None
        self._dev_info = None
        # Held by stream_frames() per-frame and by capture_image() for the full
        # grab cycle, so they never pull from the SDK queue simultaneously.
        self._lock = threading.Lock()
        self._stream_lock = threading.Lock()  # held for the lifetime of each active stream
        self._stream_cancel = threading.Event()  # set to signal the active stream to stop
        self._mode: CameraMode = CameraMode.STREAM
        self._streaming: bool = False  # True while stream_frames() generator is running
        self._stream_count: int = 0  # number of active stream_frames() generators
        # Default stream/capture resolution from camera_profiles.<model> in
        # configuration.toml, or None to use native sensor resolution.
        self._stream_size: tuple[int, int] | None = None
        self._capture_size: tuple[int, int] | None = None
        # Keep a strong reference to the ctypes callback so it isn't GC'd.
        self._connection_cb = None
        # The "capture profile" exposure: what stills are taken with, and what the
        # settings page edits and saves.
        # [ae_state, ae_target, exposure_us, auto_gain, analog_gain_raw].
        # With config.MANUAL_EXPOSURE_CAPTURE_ONLY and a manual profile, live
        # streams run on auto-exposure instead (_stream_ae_active) and the
        # profile is applied only around a capture. Ordering: _lock, then
        # _exposure_lock, never the reverse.
        self._capture_exposure: list = [1, 100, 30000.0, 1, 16]
        self._stream_ae_active: bool = False
        self._exposure_lock = threading.RLock()
        # Draft vs production. The settings page edits a draft that lives on the
        # hardware; _production holds the values real captures must use, and
        # capture_image() swaps them in around the grab while a draft exists.
        # Both are guarded by _exposure_lock.
        self._production: dict = {}
        self._draft_dirty: bool = False
        # Ranges the SDK's AE may roam over when exposure / gain are "auto".
        # A fixed value is expressed by collapsing the matching range to it.
        self._ae_exposure_range: tuple[float, float] = (100.0, 2_000_000.0)
        self._ae_gain_range: tuple[int, int] = (16, 128)

    def open(self) -> None:
        global _sdk_initialized, _dev_list_cache
        if not _MVSDK_AVAILABLE:
            raise RuntimeError(
                "MindVision SDK (mvsdk) or its dependencies are not available."
            )
        if self._h_camera is not None:
            return

        if not _sdk_initialized:
            mvsdk.CameraSdkInit(0)
            # Tell the SDK where to find .mvdat files and where to write runtime data.
            # Must be called before CameraInit; defaults to CWD which breaks when the app
            # is started from a directory other than the project root.
            mvsdk.CameraSetDataDirectory(str(self._project_root / "MindVisionCamera"))
            _sdk_initialized = True

        # Enumerate once and cache. Re-enumerating after some cameras are
        # already initialized can return a different order or omit initialized
        # devices, causing this index to map to the wrong physical camera.
        if _dev_list_cache is None:
            _dev_list_cache = mvsdk.CameraEnumerateDevice()

        dev_list = _dev_list_cache
        if len(dev_list) <= self._camera_index:
            raise RuntimeError(
                f"MindVision camera index {self._camera_index} not found "
                f"({len(dev_list)} device(s) detected)"
            )

        dev_info = dev_list[self._camera_index]
        self._dev_info = dev_info

        # Detect first run before CameraInit: if no per-serial config file exists the
        # SDK will use hardware defaults (which may have AE disabled). We seed sensible
        # defaults once and save them so all subsequent starts load from the SDK config.
        _sn_pre = dev_info.GetSn()
        _config_file = (
            self._project_root / "MindVisionCamera" / "Configs" / f"{_sn_pre}-Group0.config"
        )
        _first_run = not _config_file.exists()

        try:
            # PARAM_MODE_BY_SN (2) loads Configs/<serial>-Group0.config if it exists,
            # falling back to defaults on first run. PARAMETER_TEAM_A (0) is where
            # CameraSaveParameter writes after WB calibration.
            h = mvsdk.CameraInit(dev_info, 2, 0)
        except mvsdk.CameraException as e:
            raise RuntimeError(
                f"CameraInit failed ({e.error_code}): {e.message}"
            ) from e

        cap = mvsdk.CameraGetCapability(h)
        self._cap = cap
        self._mono = cap.sIspCapacity.bMonoSensor != 0

        mvsdk.CameraSetIspOutFormat(
            h,
            mvsdk.CAMERA_MEDIA_TYPE_MONO8 if self._mono else mvsdk.CAMERA_MEDIA_TYPE_BGR8,
        )

        # IMPORTANT: CameraSetTriggerMode resets the AE state to manual on every
        # call (verified empirically — it clears the auto-exposure flag). So the
        # trigger mode must always be set BEFORE auto-exposure, never after, or
        # AE gets silently clobbered back to off.
        self._ae_gain_range = (
            int(cap.sExposeDesc.uiAnalogGainMin), int(cap.sExposeDesc.uiAnalogGainMax)
        )
        if _first_run:
            mvsdk.CameraSetTriggerMode(h, 1)  # software trigger; continuous only while streaming
            self._ae_exposure_range = self._default_ae_exposure_range(h)
            self._capture_exposure = [1, 100, 30000.0, 1, self._ae_gain_range[0]]
            self._write_capture_exposure(h)
            mvsdk.CameraSaveParameter(h, 0)
        else:
            # CameraLoadParameter restores all saved params (including trigger
            # mode), so reload Team A config first, then re-assert software
            # trigger, then apply AE/exposure last so the trigger-mode change
            # doesn't reset them.
            try:
                mvsdk.CameraLoadParameter(h, 0)
            except Exception:
                pass
            _ae_state, _ae_target, _exp_time = _parse_exposure_config(_config_file)
            # Re-assert software trigger — CameraLoadParameter may have restored
            # continuous mode (0) from a previous stream session's saved config.
            mvsdk.CameraSetTriggerMode(h, 1)
            self._capture_exposure = self._load_capture_profile(
                h, _ae_state, _ae_target, _exp_time,
            )
            self._write_capture_exposure(h)
        self._stream_ae_active = False

        # CameraPlay starts the SDK's internal grab thread; subsequent
        # CameraGetImageBuffer calls pull from its ring buffer.
        mvsdk.CameraPlay(h)

        self._mode = CameraMode.STREAM

        channels = 1 if self._mono else 3
        buf_size = (
            cap.sResolutionRange.iWidthMax
            * cap.sResolutionRange.iHeightMax
            * channels
        )
        self._frame_buffer = mvsdk.CameraAlignMalloc(buf_size, 16)
        self._h_camera = h
        self._production = self.current_settings()
        self._draft_dirty = False

        # Register connection-status callback so we get explicit log entries
        # when USB drops rather than only seeing C++ bulk-transfer errors.
        friendly = dev_info.GetFriendlyName()
        sn = dev_info.GetSn()

        # Same model-keyed profile lookup picamera2 uses for stream_size /
        # capture_size — falls back to native sensor resolution if the model
        # isn't listed in camera_profiles.
        profile = config.get_camera_profile(friendly)
        profile_stream_size = profile.get("stream_size")
        self._stream_size = tuple(profile_stream_size) if profile_stream_size else None
        profile_capture_size = profile.get("capture_size")
        self._capture_size = tuple(profile_capture_size) if profile_capture_size else None

        def _on_connection(h_cam, msg, u_param, p_ctx):
            if msg == 0:
                logger.warning(
                    "mindvision_camera_disconnected",
                    device=friendly, sn=sn, camera_index=self._camera_index,
                )
            elif msg == 1:
                logger.info(
                    "mindvision_camera_reconnected",
                    device=friendly, sn=sn, camera_index=self._camera_index,
                )

        self._connection_cb = mvsdk.CAMERA_CONNECTION_STATUS_CALLBACK(_on_connection)
        mvsdk.CameraSetConnectionStatusCallback(h, self._connection_cb)

        logger.info(
            "mindvision_camera_initialized",
            device=friendly, sn=sn,
            mono=self._mono,
            max_width=cap.sResolutionRange.iWidthMax,
            max_height=cap.sResolutionRange.iHeightMax,
        )

        # Test grab: verify the camera is actually delivering frames after init.
        # Uses a short timeout so it doesn't stall startup if the USB link is bad.
        try:
            mvsdk.CameraSoftTrigger(h)
            raw, head = mvsdk.CameraGetImageBuffer(h, 800)
            mvsdk.CameraReleaseImageBuffer(h, raw)
            stat = mvsdk.CameraGetFrameStatistic(h)
            logger.info(
                "mindvision_camera_test_grab_ok",
                device=friendly, sn=sn,
                width=head.iWidth, height=head.iHeight,
                frames_total=stat.iTotal, frames_lost=stat.iLost,
            )
        except mvsdk.CameraException as e:
            stat = mvsdk.CameraGetFrameStatistic(h)
            logger.warning(
                "mindvision_camera_test_grab_failed",
                device=friendly, sn=sn,
                error_code=e.error_code, message=e.message,
                frames_total=stat.iTotal, frames_lost=stat.iLost,
            )

    @property
    def mode(self) -> CameraMode:
        return self._mode

    def set_trigger_mode(self, mode: int) -> None:
        """Set the SDK trigger mode while preserving the auto-exposure state.

        CameraSetTriggerMode resets the AE state to manual on every call, so we
        snapshot AE / AE target / exposure beforehand and restore them after.
        All trigger-mode changes (mode switches, stream start/stop) must go
        through here so auto-exposure survives them.
        """
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        h = self._h_camera
        try:
            ae = mvsdk.CameraGetAeState(h)
            target = mvsdk.CameraGetAeTarget(h)
            exp = mvsdk.CameraGetExposureTime(h)
        except Exception:
            ae = target = exp = None

        mvsdk.CameraSetTriggerMode(h, mode)

        if ae is not None:
            try:
                mvsdk.CameraSetAeState(h, ae)
                mvsdk.CameraSetAeTarget(h, target)
                if not ae:
                    mvsdk.CameraSetExposureTime(h, exp)
            except Exception:
                logger.warning("mindvision_reapply_ae_after_trigger_failed")

    # ── Capture profile vs. stream exposure ──────────────────────────────────
    # The camera has one exposure state. The capture profile (saved manual
    # values) is what stills use; while a live stream runs and the profile is
    # manual, the camera is switched to auto-exposure so the preview stays
    # usable, and the profile is swapped back in around each capture / save /
    # white-balance calibration.

    def _default_ae_exposure_range(self, h: int) -> tuple[float, float]:
        """AE exposure range the SDK currently holds, unless it is collapsed.

        A collapsed range (min == max) is a saved fixed-exposure/auto-gain
        profile, not a usable auto range, so fall back to the sensor range
        capped at 2 s (the settings page's exposure limit).
        """
        try:
            lo, hi = mvsdk.CameraGetAeExposureRange(h)
            if hi - lo > 1.0:
                return float(lo), float(hi)
        except Exception:
            pass
        try:
            lo, hi, _ = mvsdk.CameraGetExposureTimeRange(h)
            return float(lo), float(min(hi, 2_000_000.0))
        except Exception:
            return 100.0, 2_000_000.0

    def _load_capture_profile(
        self, h: int, ae_state: int, ae_target: int, exp_time: float,
    ) -> list:
        """Rebuild the capture profile from the loaded SDK parameters.

        The gain mode isn't stored as its own flag; it's encoded in the AE ranges
        the SDK persists (user_ae_{min,max}_{exposure_time,analog_gain}):
          AE on, gain range collapsed      -> auto exposure, fixed gain
          AE on, exposure range collapsed  -> fixed exposure, auto gain
        """
        gain_lo, gain_hi = self._ae_gain_range
        try:
            gain = int(mvsdk.CameraGetAnalogGain(h))
        except Exception:
            gain = gain_lo
        auto_gain = 1 if ae_state else 0
        exp_lo = exp_hi = None
        if ae_state:
            try:
                exp_lo, exp_hi = mvsdk.CameraGetAeExposureRange(h)
                ae_gain_lo, ae_gain_hi = mvsdk.CameraGetAeAnalogGainRange(h)
                if ae_gain_lo == ae_gain_hi:
                    auto_gain, gain = 0, int(ae_gain_lo)
            except Exception:
                pass
        self._ae_exposure_range = self._default_ae_exposure_range(h)
        if ae_state and auto_gain and exp_lo is not None and exp_hi - exp_lo <= 1.0:
            ae_state, exp_time = 0, float(exp_lo)
        gain = min(max(gain, gain_lo), gain_hi)
        return [ae_state, ae_target, exp_time, auto_gain, gain]

    @property
    def capture_exposure(self) -> dict:
        ae, target, exp, auto_gain, gain = self._capture_exposure
        return {
            "ae_enabled": bool(ae), "ae_target": target, "exposure_us": exp,
            "auto_gain": bool(auto_gain), "analog_gain": gain,
        }

    def _stream_override_wanted(self) -> bool:
        return (
            config.MANUAL_EXPOSURE_CAPTURE_ONLY
            and not self._capture_exposure[0]
            and self._mode != CameraMode.HARDWARE_TRIGGER
        )

    def _write_capture_exposure(self, h: int | None = None) -> None:
        """Put the capture profile on the camera.

        The SDK has no separate auto-gain switch: AE drives exposure and analog
        gain together within CameraSetAe{Exposure,AnalogGain}Range. Fixed gain
        collapses the gain range to one value; fixed exposure with auto gain
        keeps AE on with the exposure range collapsed instead.
        """
        h = self._h_camera if h is None else h
        ae, target, exp, auto_gain, gain = self._capture_exposure
        if ae or auto_gain:
            exp_range = self._ae_exposure_range if ae else (exp, exp)
            gain_range = self._ae_gain_range if auto_gain else (gain, gain)
            mvsdk.CameraSetAeExposureRange(h, *exp_range)
            mvsdk.CameraSetAeAnalogGainRange(h, *gain_range)
            # Set fixed values before AE goes on: writing exposure while AE is
            # on can make some SDK builds silently drop AE.
            if not ae:
                mvsdk.CameraSetExposureTime(h, exp)
            if not auto_gain:
                mvsdk.CameraSetAnalogGain(h, gain)
            mvsdk.CameraSetAeState(h, 1)
            mvsdk.CameraSetAeTarget(h, target)
        else:
            # Leave the AE ranges open so a saved manual profile doesn't persist
            # collapsed ranges that would later read back as a fixed-gain profile.
            mvsdk.CameraSetAeExposureRange(h, *self._ae_exposure_range)
            mvsdk.CameraSetAeAnalogGainRange(h, *self._ae_gain_range)
            mvsdk.CameraSetAeState(h, 0)
            mvsdk.CameraSetAeTarget(h, target)
            mvsdk.CameraSetExposureTime(h, exp)
            mvsdk.CameraSetAnalogGain(h, gain)

    def _write_stream_exposure(self) -> None:
        h = self._h_camera
        mvsdk.CameraSetAeExposureRange(h, *self._ae_exposure_range)
        mvsdk.CameraSetAeAnalogGainRange(h, *self._ae_gain_range)
        mvsdk.CameraSetAeState(h, 1)
        mvsdk.CameraSetAeTarget(h, self._capture_exposure[1])

    def begin_stream_exposure(self) -> None:
        """Switch to auto-exposure for a live stream if the capture profile is manual.

        Call after set_trigger_mode(0), which would otherwise reset AE.
        """
        with self._exposure_lock:
            if self._h_camera is None or self._stream_ae_active or not self._stream_override_wanted():
                return
            self._write_stream_exposure()
            self._stream_ae_active = True
            logger.info("mindvision_stream_auto_exposure", camera_id=self._camera_index)

    def end_stream_exposure(self) -> None:
        """Put the capture profile back on the camera once no stream needs AE."""
        with self._exposure_lock:
            if not self._stream_ae_active:
                return
            self._stream_ae_active = False
            if self._h_camera is not None:
                self._write_capture_exposure()

    @contextmanager
    def capture_exposure_applied(self):
        """Temporarily apply the capture profile while a stream holds AE.

        Yields True if the profile was swapped in (the caller should let the
        exposure settle), False if it was already in effect.
        """
        with self._exposure_lock:
            swapped = self._stream_ae_active and self._h_camera is not None
            if swapped:
                self._write_capture_exposure()
            try:
                yield swapped
            finally:
                if swapped and self._stream_ae_active and self._h_camera is not None:
                    self._write_stream_exposure()

    def set_capture_exposure(
        self,
        ae_enabled: bool | None = None,
        ae_target: int | None = None,
        exposure_us: float | None = None,
        auto_gain: bool | None = None,
        analog_gain: int | None = None,
    ) -> None:
        """Update the capture profile's exposure (what the settings page edits).

        Applied to the hardware immediately unless a live stream is holding AE,
        in which case only the stored profile changes.
        """
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        with self._exposure_lock:
            if ae_enabled is not None:
                self._capture_exposure[0] = 1 if ae_enabled else 0
            if ae_target is not None:
                self._capture_exposure[1] = int(ae_target)
            if exposure_us is not None:
                self._capture_exposure[2] = float(exposure_us)
            if auto_gain is not None:
                self._capture_exposure[3] = 1 if auto_gain else 0
            if analog_gain is not None:
                lo, hi = self._ae_gain_range
                self._capture_exposure[4] = min(max(int(analog_gain), lo), hi)
            if self._streaming and self._stream_override_wanted():
                self._write_stream_exposure()
                self._stream_ae_active = True
            else:
                self._stream_ae_active = False
                self._write_capture_exposure()

    def save_parameters(self) -> None:
        """Commit the current settings (including any draft) to production and disk.

        Persists with the capture profile applied, not a stream's AE override.
        """
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        with self._exposure_lock:
            with self.capture_exposure_applied():
                mvsdk.CameraSaveParameter(self._h_camera, 0)
            self._production = self.current_settings()
            self._draft_dirty = False

    # ── Draft vs production ──────────────────────────────────────────────────
    # The camera has one set of hardware settings. Settings-page edits go onto
    # the hardware as a draft (so its preview and snapshots show them), while
    # _production keeps the saved values; capture_image() applies production
    # around every real capture, so a draft never reaches production images.

    def current_settings(self) -> dict:
        """The settings the camera currently holds (the draft, if one exists)."""
        s = read_hw_settings(self._h_camera, self._cap)
        # Exposure/gain come from the capture profile: the hardware reads back
        # a stream's auto-exposure override while one is running.
        s.update(self.capture_exposure)
        return {k: s[k] for k in SETTING_KEYS if k in s}

    def draft_changes(self) -> dict[str, tuple]:
        """{key: (production value, draft value)} for every setting that differs."""
        with self._exposure_lock:
            if not self._draft_dirty or self._h_camera is None:
                return {}
            current = self.current_settings()
            changes = {
                k: (self._production[k], current[k])
                for k in SETTING_KEYS
                if k in current and k in self._production
                and not _same_setting(k, self._production[k], current[k])
            }
            if not changes:
                self._draft_dirty = False
            return changes

    def apply_draft(self, body: dict) -> tuple[list[str], dict[str, str]]:
        """Apply settings as a draft: live on the camera, not used by captures."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if self._mode == CameraMode.HARDWARE_TRIGGER:
            # Hardware-triggered frames are exposed before capture_image() runs,
            # too late to swap production in, so no drafts in this mode.
            raise RuntimeError("Settings can't be tested while in hardware-trigger mode")
        with self._exposure_lock:
            applied, errors = apply_settings(self._h_camera, body, self)
            if applied:
                self._draft_dirty = True
            return applied, errors

    def apply_production(self, body: dict) -> tuple[list[str], dict[str, str]]:
        """Apply settings straight to production (in memory; not saved to disk)."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        with self._exposure_lock:
            applied, errors = apply_settings(self._h_camera, body, self)
            current = self.current_settings()
            for key in applied:
                if key in current:
                    self._production[key] = current[key]
            return applied, errors

    def discard_draft(self) -> list[str]:
        """Put the production settings back on the camera. Returns the keys reverted."""
        with self._exposure_lock:
            changes = self.draft_changes()
            if changes:
                apply_settings(self._h_camera, {k: p for k, (p, _) in changes.items()}, self)
            self._draft_dirty = False
            return list(changes)

    @contextmanager
    def production_applied(self):
        """Apply the production settings for the duration of a real capture.

        Yields True if a draft was swapped out (the caller should let queued
        frames settle), False if production was already in effect.
        """
        with self._exposure_lock:
            changes = self.draft_changes() if self._h_camera is not None else {}
            if not changes:
                yield False
                return
            apply_settings(self._h_camera, {k: p for k, (p, _) in changes.items()}, self)
            try:
                yield True
            finally:
                apply_settings(self._h_camera, {k: d for k, (_, d) in changes.items()}, self)

    def _persist_production(self) -> None:
        """Write the production settings to disk without committing a draft."""
        with self._exposure_lock, self.production_applied(), self.capture_exposure_applied():
            mvsdk.CameraSaveParameter(self._h_camera, 0)

    def set_mode(self, mode: CameraMode) -> None:
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if mode == CameraMode.HARDWARE_TRIGGER:
            self.set_trigger_mode(2)
        else:
            # STREAM and CAPTURE both idle in software trigger; stream_frames()
            # activates continuous mode automatically while a stream is active.
            self.set_trigger_mode(1)
        self._mode = mode
        if mode == CameraMode.HARDWARE_TRIGGER:
            # Hardware trigger uses the saved/manual exposure everywhere, and
            # production settings must already be on the camera when a trigger
            # fires, so any draft is dropped.
            self.end_stream_exposure()
            reverted = self.discard_draft()
            if reverted:
                logger.info("mindvision_draft_discarded", camera_id=self._camera_index, keys=reverted)
        logger.info("camera_mode_changed", mode=mode.value)

    def apply_config(self, key: str, value) -> None:
        """Apply a runtime config change to the live camera hardware.

        Most runtime-updatable keys (stream.*, hw_trigger.*) control upload/save
        behaviour and do not need to be applied to the camera SDK. If a key ever
        needs to translate to a live SDK call, add it here.
        """
        logger.debug("apply_config_noop", key=key, value=value)

    def get_orientation(self) -> dict:
        """Return current rotation and mirror settings from the SDK."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        rotation = mvsdk.CameraGetRotate(self._h_camera)
        h_mirror = bool(mvsdk.CameraGetMirror(self._h_camera, 0))
        v_mirror = bool(mvsdk.CameraGetMirror(self._h_camera, 1))
        return {"rotation": rotation, "h_mirror": h_mirror, "v_mirror": v_mirror}

    def set_rotation(self, rotation: int) -> None:
        """Set SDK-level rotation (0=0°, 1=90°CCW, 2=180°, 3=270°CCW) and persist."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if rotation not in (0, 1, 2, 3):
            raise ValueError(f"rotation must be 0-3, got {rotation}")
        mvsdk.CameraSetRotate(self._h_camera, rotation)
        self.save_parameters()
        logger.info("camera_rotation_set", rotation=rotation)

    def set_mirror(self, direction: int, enable: bool) -> None:
        """Set SDK-level mirror (direction: 0=horizontal, 1=vertical) and persist."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if direction not in (0, 1):
            raise ValueError(f"direction must be 0 (horizontal) or 1 (vertical), got {direction}")
        mvsdk.CameraSetMirror(self._h_camera, direction, int(enable))
        self.save_parameters()
        label = "horizontal" if direction == 0 else "vertical"
        logger.info("camera_mirror_set", direction=label, enabled=enable)

    def calibrate_white_balance(self) -> dict:
        """One-push WB calibration: match QT5 demo sequence exactly."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if self._mono or bool(mvsdk.CameraGetMonochrome(self._h_camera)):
            raise RuntimeError("White balance not applicable to monochrome cameras")

        # Calibrate under the capture exposure, not the stream's auto-exposure:
        # the gains are what stills will be taken with.
        with self.capture_exposure_applied() as swapped:
            if swapped:
                exp_us = self._capture_exposure[2]
                time.sleep(0.5 + 3 * exp_us / 1_000_000)  # let the exposure settle

            # Reset to neutral so CameraSetOnceWB sees the unbiased scene.
            # Old stored gains make the image look "already white", causing OnceWB
            # to compute near-zero correction instead of the real scene values.
            mvsdk.CameraSetWbMode(self._h_camera, False)
            mvsdk.CameraSetGain(self._h_camera, 100, 100, 100)
            time.sleep(0.3)  # wait for neutral gains to take effect in the ISP
            mvsdk.CameraSetOnceWB(self._h_camera)
            r, g, b = mvsdk.CameraGetGain(self._h_camera)
            mvsdk.CameraSetGain(self._h_camera, r, g, b)

        # Persist to Configs/<sn>-Group0.config. The new gains go to production;
        # any other draft edits stay a draft instead of being saved with them.
        with self._exposure_lock:
            self._production.update(r_gain=r, g_gain=g, b_gain=b)
            self._persist_production()
        logger.info("white_balance_calibrated", r=r, g=g, b=b)

        return {"r_gain": r, "g_gain": g, "b_gain": b}

    def close(self) -> None:
        if self._h_camera is not None:
            try:
                mvsdk.CameraUnInit(self._h_camera)
            except Exception:
                logger.exception("mindvision_close_failed")
            self._h_camera = None

        if self._frame_buffer:
            mvsdk.CameraAlignFree(self._frame_buffer)
            self._frame_buffer = 0

    def exposure_grab_timeout_ms(self) -> int:
        """Grab timeout (ms) scaled to the camera's current exposure time.

        A fixed short timeout can expire before a long exposure (auto or
        manual) finishes reading out, e.g. in low light. Callers doing a
        single triggered grab should pass this instead of relying on
        _grab_frame's short default, which assumes a frame is already
        sitting in the SDK's ring buffer (true while streaming, not
        guaranteed right after a fresh soft trigger).
        """
        try:
            exp_us = mvsdk.CameraGetExposureTime(self._h_camera)
        except Exception:
            exp_us = 0.0
        return max(2000, int(exp_us / 1000) + 1000)

    def _grab_frame(self, timeout_ms: int = 1000) -> "tuple[np.ndarray, object] | tuple[None, None]":
        """Grab one processed frame as a numpy array plus the raw SDK frame header.

        Caller must hold self._lock.
        Returns (array, head) on success, (None, None) on failure.
        """
        try:
            raw, head = mvsdk.CameraGetImageBuffer(self._h_camera, timeout_ms)
            mvsdk.CameraImageProcess(self._h_camera, raw, self._frame_buffer, head)
            mvsdk.CameraReleaseImageBuffer(self._h_camera, raw)

            channels = 1 if head.uiMediaType == mvsdk.CAMERA_MEDIA_TYPE_MONO8 else 3
            frame_data = (mvsdk.c_ubyte * head.uBytes).from_address(self._frame_buffer)
            arr = np.frombuffer(frame_data, dtype=np.uint8).reshape(
                (head.iHeight, head.iWidth, channels)
            )
            # _frame_buffer is shared C memory reused on the next grab, so we
            # must copy into a new numpy array before releasing the lock.
            return arr.copy(), head
        except mvsdk.CameraException as e:
            stat = mvsdk.CameraGetFrameStatistic(self._h_camera)
            logger.warning(
                "mindvision_grab_failed",
                error_code=e.error_code,
                message=e.message,
                timed_out=(e.error_code == mvsdk.CAMERA_STATUS_TIME_OUT),
                frames_total=stat.iTotal,
                frames_lost=stat.iLost,
            )
            return None, None

    def _discard_until_exposure_settled(self, max_frames: int = 8) -> None:
        """Drop streamed frames still carrying the stream's auto-exposure.

        Frames queued before the capture exposure took effect carry the old
        exposure in their header; stop once one matches the capture profile.
        Caller must hold self._lock.
        """
        ae, _, target_us, auto_gain, gain_raw = self._capture_exposure
        # Fixed gain must match too: a frame can carry the capture exposure
        # while still holding the gain the stream's AE left behind.
        target_gain_x = None
        if not auto_gain and self._cap is not None:
            target_gain_x = gain_raw * float(self._cap.sExposeDesc.fAnalogGainStep)
        timeout_ms = self.exposure_grab_timeout_ms()
        for _ in range(max_frames):
            try:
                raw, head = mvsdk.CameraGetImageBuffer(self._h_camera, timeout_ms)
                mvsdk.CameraReleaseImageBuffer(self._h_camera, raw)
            except mvsdk.CameraException:
                return
            gain_ok = target_gain_x is None or abs(head.fAnalogGain - target_gain_x) <= 0.02 * target_gain_x
            exp_ok = bool(ae) or abs(head.uiExpTime - target_us) <= max(50.0, 0.02 * target_us)
            if gain_ok and exp_ok:
                # This frame matches, but it's the one we just dropped; the next
                # grab (the real capture) is guaranteed to be at capture exposure.
                return

    def _snapshot_capture_state(self, head) -> dict:
        """Camera settings that produced the frame just grabbed.

        Exposure, analog gain, gamma/contrast/saturation and the RGB digital
        gains come from the frame header, which the SDK fills per frame, so
        they describe exactly this image. The auto/manual flags come from the
        capture profile: the hardware AE flag is also on for fixed exposure
        with auto gain. Caller must hold self._lock.
        """
        state: dict = {}
        if head is not None:
            state.update(
                exposure_us=int(head.uiExpTime),
                analog_gain_x=round(float(head.fAnalogGain), 4),
                gamma=int(head.iGamma),
                contrast=int(head.iContrast),
                saturation=int(head.iSaturation),
                r_gain=round(float(head.fRgain), 4),
                g_gain=round(float(head.fGgain), 4),
                b_gain=round(float(head.fBgain), 4),
            )
        state["ae_enabled"] = bool(self._capture_exposure[0])
        state["auto_gain"] = bool(self._capture_exposure[3])
        state["ae_target"] = int(self._capture_exposure[1])
        # Raw gain for this frame (multiplier / step), so it can be fed back
        # into the manual gain setting; falls back to the camera's current value.
        step = float(self._cap.sExposeDesc.fAnalogGainStep) if self._cap is not None else 0.0
        if head is not None and step > 0:
            state["analog_gain_raw"] = round(float(head.fAnalogGain) / step)
        else:
            try:
                state["analog_gain_raw"] = int(mvsdk.CameraGetAnalogGain(self._h_camera))
            except Exception:
                state["analog_gain_raw"] = None
        return state

    def _build_exif(self, captured_at: str, state: dict | None = None) -> bytes | None:
        """Build a piexif EXIF blob from the settings captured with the frame.

        ExposureTime holds the headline value; the full snapshot (including
        analog gain) is written as JSON in UserComment. ISOSpeedRatings is left
        out on purpose: the camera has analog gain, not a calibrated ISO.
        """
        try:
            import json

            import piexif

            state = state or {}
            exp_us = int(state.get("exposure_us") or 0)

            model = ""
            serial = ""
            if self._dev_info is not None:
                try:
                    model = self._dev_info.GetFriendlyName() or ""
                except Exception:
                    pass
                try:
                    serial = self._dev_info.GetSn() or ""
                except Exception:
                    pass

            # EXIF datetime format: "YYYY:MM:DD HH:MM:SS"
            try:
                dt = datetime.fromisoformat(captured_at)
                exif_dt = dt.strftime("%Y:%m:%d %H:%M:%S").encode()
            except Exception:
                exif_dt = b""

            exif_dict = {
                "0th": {
                    piexif.ImageIFD.Make: b"MindVision",
                    piexif.ImageIFD.Model: model.encode(),
                    piexif.ImageIFD.DateTime: exif_dt,
                    piexif.ImageIFD.CameraSerialNumber: serial.encode(),
                },
                "Exif": {
                    piexif.ExifIFD.DateTimeOriginal: exif_dt,
                    piexif.ExifIFD.ExposureTime: (exp_us, 1_000_000),
                    # Standard EXIF tag so any viewer shows AE: 0 = auto, 1 = manual.
                    piexif.ExifIFD.ExposureMode: 0 if state.get("ae_enabled") else 1,
                    piexif.ExifIFD.BodySerialNumber: serial.encode(),
                    piexif.ExifIFD.UserComment: (
                        b"ASCII\x00\x00\x00"
                        + json.dumps(state, sort_keys=True).encode("ascii")
                    ),
                },
                "GPS": {},
                "1st": {},
            }
            return piexif.dump(exif_dict)
        except Exception:
            logger.warning("exif_build_failed")
            return None

    def _encode_jpeg(
        self,
        frame: "np.ndarray",
        quality: int,
        exif_bytes: bytes | None = None,
        resize: tuple[int, int] | None = None,
    ) -> bytes:
        # EXIF embedding requires PIL; only the capture path (not the streaming
        # hot path) needs it, so it's the one place we pay the PIL conversion cost.
        if exif_bytes:
            if frame.ndim == 3 and frame.shape[2] == 1:
                img = PilImage.fromarray(frame[:, :, 0], mode="L")
            else:
                img = PilImage.fromarray(frame[:, :, ::-1])  # SDK outputs BGR; PIL expects RGB
            if resize is not None and resize != img.size:
                img = img.resize(resize, PilImage.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, exif=exif_bytes)
            return buf.getvalue()

        if frame.ndim == 3 and frame.shape[2] == 1:
            frame = frame[:, :, 0]
        if resize is not None and (frame.shape[1], frame.shape[0]) != resize:
            frame = cv2.resize(frame, resize, interpolation=cv2.INTER_LINEAR)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("Failed to JPEG-encode frame")
        return encoded.tobytes()

    def stream_frames(
        self,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
    ):
        frame_interval = 1.0 / (fps if fps is not None else config.STREAM_FPS)
        self._stream_count += 1
        self._streaming = True
        self._stream_cancel.clear()
        _continuous_active = False  # whether we've switched to continuous for this session
        try:
            while not self._stream_cancel.is_set():
                if self._h_camera is None:
                    _continuous_active = False
                    try:
                        self.open()
                    except RuntimeError:
                        logger.warning("mindvision_stream_waiting")
                        time.sleep(2)
                        continue

                if not _continuous_active and self._mode != CameraMode.HARDWARE_TRIGGER:
                    self.set_trigger_mode(0)  # continuous while streaming (preserves AE)
                    self.begin_stream_exposure()  # manual capture exposure -> AE for the preview
                    _continuous_active = True

                start = time.monotonic()
                frame_data = None

                with self._lock:
                    frame, _head = self._grab_frame(timeout_ms=self.exposure_grab_timeout_ms())
                    if frame is not None:
                        native_height, native_width = frame.shape[:2]
                        if width is not None and height is not None:
                            resize = (width, height)
                        elif width is not None:
                            # Derive the missing dimension from the native aspect.
                            # Falling back to the native height instead squashed the
                            # frame: a 2448x2048 sensor asked for width=640 returned
                            # 640x2048, so callers that pass only one dimension got a
                            # distorted preview rather than a scaled one.
                            resize = (width, max(1, round(width * native_height / native_width)))
                        elif height is not None:
                            resize = (max(1, round(height * native_width / native_height)), height)
                        elif self._stream_size is not None and self._stream_size != (native_width, native_height):
                            resize = self._stream_size
                        else:
                            resize = None
                        frame_data = self._encode_jpeg(frame, config.STREAM_QUALITY, resize=resize)

                if frame_data:
                    yield frame_data

                elapsed = time.monotonic() - start
                remaining = frame_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            self._stream_count -= 1
            self._streaming = self._stream_count > 0
            # Only revert trigger mode when the last active generator exits.
            # If another generator is still running it already set continuous mode
            # and reverting here would break it.
            if self._stream_count == 0 and self._h_camera is not None and self._mode != CameraMode.HARDWARE_TRIGGER:
                try:
                    self.set_trigger_mode(1)  # back to software trigger (preserves AE)
                    logger.info("stream_ended_reverted_to_software_trigger")
                except Exception:
                    logger.warning("mindvision_revert_trigger_failed")
            if self._stream_count == 0:
                try:
                    self.end_stream_exposure()  # restore the manual capture exposure
                except Exception:
                    logger.warning("mindvision_restore_capture_exposure_failed")

    def capture_image(
        self,
        resolution: tuple[int, int] | None = None,
        output_folder: Path = config.CAPTURE_TMP_DIR,
    ) -> tuple[Path, CaptureMetrics]:
        if self._h_camera is None:
            self.open()

        target_resolution = resolution or self._capture_size

        captured_at = datetime.now(timezone.utc).isoformat()
        # The name must be unique per camera AND per call. capture_all grabs every
        # camera concurrently into one shared output_folder, so a name built only
        # from whole seconds collided: all three writes landed on the same path,
        # the last one won, and the zip shipped that single frame three times
        # under camera_0/1/2.jpg. The failure was silent and produced three
        # database rows, one per camera serial, all holding the same image.
        output_image = output_folder / f"cam{self._camera_index}_{time.time_ns()}.jpg"

        # Holding _lock for the whole swap keeps the stream from serving (or
        # stealing) frames taken at the wrong settings. production_applied()
        # swaps out a settings-page draft; capture_exposure_applied() swaps
        # out a stream's auto-exposure override.
        with self._lock, self.production_applied() as draft_swapped, \
                self.capture_exposure_applied() as swapped:
            if not self._streaming and self._mode != CameraMode.HARDWARE_TRIGGER:
                mvsdk.CameraSoftTrigger(self._h_camera)
            t0 = time.perf_counter()
            if swapped or (draft_swapped and self._streaming):
                self._discard_until_exposure_settled()
            frame, head = self._grab_frame(timeout_ms=self.exposure_grab_timeout_ms())
            capture_duration_ms = (time.perf_counter() - t0) * 1000
            # Read the camera state while the capture profile is still applied
            # and the lock is held; after this block a running stream may swap
            # auto-exposure back in and the values would describe the stream.
            exif_state = self._snapshot_capture_state(head) if frame is not None else None

        if frame is None:
            raise RuntimeError("Failed to capture frame from MindVision camera")

        native_height, native_width = frame.shape[:2]
        resize = (
            target_resolution
            if target_resolution is not None and target_resolution != (native_width, native_height)
            else None
        )

        exif_bytes = self._build_exif(captured_at, exif_state)
        jpeg_bytes = self._encode_jpeg(frame, quality=95, exif_bytes=exif_bytes, resize=resize)
        output_image.write_bytes(jpeg_bytes)

        width, height = resize or (native_width, native_height)
        metrics = CaptureMetrics(
            captured_at=captured_at,
            capture_duration_ms=capture_duration_ms,
            width=width,
            height=height,
            file_size_bytes=output_image.stat().st_size,
        )
        return output_image, metrics

    @property
    def camera_index(self) -> int:
        return self._camera_index

    @property
    def serial_number(self) -> str | None:
        return self._dev_info.GetSn() if self._dev_info is not None else None

    def camera_info(self) -> dict:
        if self._h_camera is None or self._dev_info is None:
            return {"type": "mindvision", "camera_id": self._camera_index, "status": "closed"}
        return {
            "type": "mindvision",
            "camera_id": self._camera_index,
            "serial_number": self._dev_info.GetSn(),
            "model": self._dev_info.GetFriendlyName(),
            "product_name": self._dev_info.GetProductName(),
            "port_type": self._dev_info.GetPortType(),
            "mono": self._mono,
            "max_width": self._cap.sResolutionRange.iWidthMax,
            "max_height": self._cap.sResolutionRange.iHeightMax,
            # From camera_profiles.<model> as loaded at process start (see
            # config.py — the file isn't re-read after startup, so this
            # reflects what's actually in effect, not necessarily what's
            # currently on disk). None means "no profile entry, falls back
            # to native resolution".
            "capture_size": self._capture_size,
            "stream_size": self._stream_size,
        }


def capture_many(
    cameras: dict[int, "MindVisionCamera"],
    cam_ids: list[int],
    tmp_dir: Path,
    timeout: float = 15,
) -> tuple[dict[int, tuple[Path, CaptureMetrics]], dict[int, str], list[int]]:
    """Capture one frame from each of the given cameras concurrently.

    Returns (results, errors, timed_out_camera_ids): results maps camera_id to
    (path, metrics) for cameras that succeeded; errors maps camera_id to the
    exception message for cameras that raised; timed_out_camera_ids lists
    cameras whose capture thread didn't finish within `timeout` seconds.
    """
    results: dict[int, tuple[Path, CaptureMetrics]] = {}
    errors: dict[int, str] = {}
    mu = threading.Lock()

    def grab_one(cam_id: int, cam: "MindVisionCamera") -> None:
        try:
            path, metrics = cam.capture_image(output_folder=tmp_dir)
            with mu:
                results[cam_id] = (path, metrics)
        except Exception as exc:
            with mu:
                errors[cam_id] = str(exc)

    threads = [
        threading.Thread(target=grab_one, args=(cid, cameras[cid]), daemon=True)
        for cid in cam_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    timed_out = [cid for cid, t in zip(cam_ids, threads) if t.is_alive()]
    return results, errors, timed_out
