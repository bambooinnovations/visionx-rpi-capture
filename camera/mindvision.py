from __future__ import annotations

import io
import threading
import time
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
        if _first_run:
            mvsdk.CameraSetTriggerMode(h, 1)  # software trigger; continuous only while streaming
            mvsdk.CameraSetAeState(h, 1)
            mvsdk.CameraSetAeTarget(h, 100)
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
            mvsdk.CameraSetAeState(h, _ae_state)
            mvsdk.CameraSetAeTarget(h, _ae_target)
            if not _ae_state:
                mvsdk.CameraSetExposureTime(h, _exp_time)

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
        mvsdk.CameraSaveParameter(self._h_camera, 0)
        logger.info("camera_rotation_set", rotation=rotation)

    def set_mirror(self, direction: int, enable: bool) -> None:
        """Set SDK-level mirror (direction: 0=horizontal, 1=vertical) and persist."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if direction not in (0, 1):
            raise ValueError(f"direction must be 0 (horizontal) or 1 (vertical), got {direction}")
        mvsdk.CameraSetMirror(self._h_camera, direction, int(enable))
        mvsdk.CameraSaveParameter(self._h_camera, 0)
        label = "horizontal" if direction == 0 else "vertical"
        logger.info("camera_mirror_set", direction=label, enabled=enable)

    def calibrate_white_balance(self) -> dict:
        """One-push WB calibration: match QT5 demo sequence exactly."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if self._mono or bool(mvsdk.CameraGetMonochrome(self._h_camera)):
            raise RuntimeError("White balance not applicable to monochrome cameras")

        # Reset to neutral so CameraSetOnceWB sees the unbiased scene.
        # Old stored gains make the image look "already white", causing OnceWB
        # to compute near-zero correction instead of the real scene values.
        mvsdk.CameraSetWbMode(self._h_camera, False)
        mvsdk.CameraSetGain(self._h_camera, 100, 100, 100)
        time.sleep(0.3)  # wait for neutral gains to take effect in the ISP
        mvsdk.CameraSetOnceWB(self._h_camera)
        r, g, b = mvsdk.CameraGetGain(self._h_camera)
        mvsdk.CameraSetGain(self._h_camera, r, g, b)

        mvsdk.CameraSaveParameter(self._h_camera, 0)  # persist to Configs/<sn>-Group0.config
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

    def _build_exif(self, captured_at: str) -> bytes | None:
        """Build a piexif EXIF blob by querying live camera state from the SDK."""
        try:
            import piexif

            # Query actual exposure time and gain from the SDK.
            # The frame header's iExpTime is unreliable in hardware trigger mode.
            exp_us = 0
            gain_raw = 100
            if self._h_camera is not None:
                try:
                    exp_us = int(mvsdk.CameraGetExposureTime(self._h_camera))
                except Exception:
                    pass
                try:
                    # CameraGetAnalogGain returns the current analog gain value
                    gain_raw = int(mvsdk.CameraGetAnalogGain(self._h_camera))
                except Exception:
                    pass

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
                    piexif.ExifIFD.ISOSpeedRatings: gain_raw,
                    piexif.ExifIFD.BodySerialNumber: serial.encode(),
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

        with self._lock:
            if not self._streaming and self._mode != CameraMode.HARDWARE_TRIGGER:
                mvsdk.CameraSoftTrigger(self._h_camera)
            t0 = time.perf_counter()
            frame, head = self._grab_frame(timeout_ms=self.exposure_grab_timeout_ms())
            capture_duration_ms = (time.perf_counter() - t0) * 1000

        if frame is None:
            raise RuntimeError("Failed to capture frame from MindVision camera")

        native_height, native_width = frame.shape[:2]
        resize = (
            target_resolution
            if target_resolution is not None and target_resolution != (native_width, native_height)
            else None
        )

        exif_bytes = self._build_exif(captured_at)
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
