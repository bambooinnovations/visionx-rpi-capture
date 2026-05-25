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
    import numpy as np
    from PIL import Image as PilImage
    import mvsdk

    _MVSDK_AVAILABLE = True
except (ImportError, OSError):
    _MVSDK_AVAILABLE = False

# Guards so CameraSdkInit / CameraSetDataDirectory are only called once
# regardless of how many MindVisionCamera instances are opened.
_sdk_initialized = False

import config
from camera.base import BaseCamera
from metrics import CaptureMetrics

logger = structlog.get_logger()


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
        self._mode: CameraMode = CameraMode.STREAM
        self._streaming: bool = False  # True while stream_frames() generator is running
        # Keep a strong reference to the ctypes callback so it isn't GC'd.
        self._connection_cb = None

    def open(self) -> None:
        global _sdk_initialized
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

        dev_list = mvsdk.CameraEnumerateDevice()
        if len(dev_list) <= self._camera_index:
            raise RuntimeError(
                f"MindVision camera index {self._camera_index} not found "
                f"({len(dev_list)} device(s) detected)"
            )

        dev_info = dev_list[self._camera_index]
        self._dev_info = dev_info

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
        mvsdk.CameraSetTriggerMode(h, 1)  # software trigger; continuous only while streaming

        if config.MV_AUTO_EXPOSURE:
            mvsdk.CameraSetAeState(h, 1)
        else:
            mvsdk.CameraSetAeState(h, 0)
            mvsdk.CameraSetExposureTime(h, config.MV_EXPOSURE_US)

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

    def set_mode(self, mode: CameraMode) -> None:
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if mode == CameraMode.HARDWARE_TRIGGER:
            mvsdk.CameraSetTriggerMode(self._h_camera, 2)
        else:
            # STREAM and CAPTURE both idle in software trigger; stream_frames()
            # activates continuous mode automatically while a stream is active.
            mvsdk.CameraSetTriggerMode(self._h_camera, 1)
        self._mode = mode
        logger.info("camera_mode_changed", mode=mode.value)

    def apply_config(self, key: str, value) -> None:
        """Apply a runtime config change to the live camera hardware."""
        if self._h_camera is None:
            return
        if key == "camera.mv_exposure_us":
            mvsdk.CameraSetExposureTime(self._h_camera, int(value))
        elif key == "camera.mv_auto_exposure":
            mvsdk.CameraSetAeState(self._h_camera, 1 if value else 0)

    def calibrate_white_balance(self) -> dict:
        """One-push WB calibration: match QT5 demo sequence exactly."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if self._mono:
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

    def _grab_frame(self) -> "np.ndarray | None":
        """Grab one processed frame as a numpy array. Caller must hold self._lock."""
        try:
            raw, head = mvsdk.CameraGetImageBuffer(self._h_camera, 1000)
            mvsdk.CameraImageProcess(self._h_camera, raw, self._frame_buffer, head)
            mvsdk.CameraReleaseImageBuffer(self._h_camera, raw)

            channels = 1 if head.uiMediaType == mvsdk.CAMERA_MEDIA_TYPE_MONO8 else 3
            frame_data = (mvsdk.c_ubyte * head.uBytes).from_address(self._frame_buffer)
            arr = np.frombuffer(frame_data, dtype=np.uint8).reshape(
                (head.iHeight, head.iWidth, channels)
            )
            # _frame_buffer is shared C memory reused on the next grab, so we
            # must copy into a new numpy array before releasing the lock.
            return arr.copy()
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
            return None

    def _encode_jpeg(self, frame: "np.ndarray", quality: int) -> bytes:
        if frame.ndim == 3 and frame.shape[2] == 1:
            img = PilImage.fromarray(frame[:, :, 0], mode="L")
        else:
            img = PilImage.fromarray(frame[:, :, ::-1])  # SDK outputs BGR; PIL expects RGB
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def stream_frames(self):
        frame_interval = 1.0 / config.STREAM_FPS
        self._streaming = True
        _continuous_active = False  # whether we've switched to continuous for this session
        try:
            while True:
                if self._h_camera is None:
                    _continuous_active = False
                    try:
                        self.open()
                    except RuntimeError:
                        logger.warning("mindvision_stream_waiting")
                        time.sleep(2)
                        continue

                if not _continuous_active and self._mode != CameraMode.HARDWARE_TRIGGER:
                    h = self._h_camera
                    assert h is not None
                    mvsdk.CameraSetTriggerMode(h, 0)  # continuous while streaming
                    _continuous_active = True

                start = time.monotonic()
                frame_data = None

                with self._lock:
                    frame = self._grab_frame()
                    if frame is not None:
                        frame_data = self._encode_jpeg(frame, config.STREAM_QUALITY)

                if frame_data:
                    yield frame_data

                elapsed = time.monotonic() - start
                remaining = frame_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            self._streaming = False
            if self._h_camera is not None and self._mode != CameraMode.HARDWARE_TRIGGER:
                try:
                    mvsdk.CameraSetTriggerMode(self._h_camera, 1)  # back to software trigger
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

        if resolution is not None:
            logger.warning("mindvision_resolution_param_ignored", requested=resolution)

        captured_at = datetime.now(timezone.utc).isoformat()
        output_image = output_folder / f"{int(time.time())}.jpg"

        with self._lock:
            if not self._streaming and self._mode != CameraMode.HARDWARE_TRIGGER:
                mvsdk.CameraSoftTrigger(self._h_camera)
            t0 = time.perf_counter()
            frame = self._grab_frame()
            capture_duration_ms = (time.perf_counter() - t0) * 1000

        if frame is None:
            raise RuntimeError("Failed to capture frame from MindVision camera")

        jpeg_bytes = self._encode_jpeg(frame, quality=95)
        output_image.write_bytes(jpeg_bytes)

        height, width = frame.shape[:2]
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
        }
