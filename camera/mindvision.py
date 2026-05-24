from __future__ import annotations

import io
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

try:
    import numpy as np
    from PIL import Image as PilImage
    import mvsdk

    _MVSDK_AVAILABLE = True
except (ImportError, OSError):
    _MVSDK_AVAILABLE = False

import config
from camera.base import BaseCamera
from metrics import CaptureMetrics

logger = structlog.get_logger()


class MindVisionCamera(BaseCamera):
    def __init__(self, camera_index: int = 0) -> None:
        self._camera_index = camera_index
        self._h_camera: int | None = None
        self._frame_buffer: int = 0  # aligned C buffer; 0 means not yet allocated
        self._mono: bool = False
        self._cap = None
        self._dev_info = None
        # Held by stream_frames() per-frame and by capture_image() for the full
        # grab cycle, so they never pull from the SDK queue simultaneously.
        self._lock = threading.Lock()

    def open(self) -> None:
        if not _MVSDK_AVAILABLE:
            raise RuntimeError(
                "MindVision SDK (mvsdk) or its dependencies are not available."
            )
        if self._h_camera is not None:
            return

        dev_list = mvsdk.CameraEnumerateDevice()
        if len(dev_list) <= self._camera_index:
            raise RuntimeError(
                f"MindVision camera index {self._camera_index} not found "
                f"({len(dev_list)} device(s) detected)"
            )

        dev_info = dev_list[self._camera_index]
        self._dev_info = dev_info

        try:
            h = mvsdk.CameraInit(dev_info, -1, -1)
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
        mvsdk.CameraSetTriggerMode(h, 0)  # continuous

        if config.MV_AUTO_EXPOSURE:
            mvsdk.CameraSetAeState(h, 1)
        else:
            mvsdk.CameraSetAeState(h, 0)
            mvsdk.CameraSetExposureTime(h, config.MV_EXPOSURE_US)

        # CameraPlay starts the SDK's internal grab thread; subsequent
        # CameraGetImageBuffer calls pull from its ring buffer.
        mvsdk.CameraPlay(h)

        if not self._mono:
            self._apply_white_balance(h)

        channels = 1 if self._mono else 3
        buf_size = (
            cap.sResolutionRange.iWidthMax
            * cap.sResolutionRange.iHeightMax
            * channels
        )
        self._frame_buffer = mvsdk.CameraAlignMalloc(buf_size, 16)
        self._h_camera = h

        logger.info(
            "mindvision_camera_initialized",
            device=dev_info.GetFriendlyName(),
            mono=self._mono,
            max_width=cap.sResolutionRange.iWidthMax,
            max_height=cap.sResolutionRange.iHeightMax,
        )

    def _apply_white_balance(self, h: int) -> None:
        import calibration
        wb = calibration.load().get("white_balance")
        if wb:
            mvsdk.CameraSetWbMode(h, False)
            mvsdk.CameraSetGain(h, wb["r_gain"], wb["g_gain"], wb["b_gain"])
            logger.info("white_balance_applied", r=wb["r_gain"], g=wb["g_gain"], b=wb["b_gain"])
        elif config.MV_AUTO_WB:
            mvsdk.CameraSetWbMode(h, True)
            logger.info("white_balance_auto")

    def calibrate_white_balance(self) -> dict:
        """One-push WB calibration: compute gains for current lighting, persist, return them."""
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        if self._mono:
            raise RuntimeError("White balance not applicable to monochrome cameras")

        mvsdk.CameraSetOnceWB(self._h_camera)
        time.sleep(0.3)  # allow the SDK's grab thread to process a frame with the new gains

        r, g, b = mvsdk.CameraGetGain(self._h_camera)

        import calibration
        calibration.save("white_balance", {"r_gain": r, "g_gain": g, "b_gain": b})
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
            raw, head = mvsdk.CameraGetImageBuffer(self._h_camera, 200)
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
            if e.error_code != mvsdk.CAMERA_STATUS_TIME_OUT:
                logger.warning(
                    "mindvision_grab_failed",
                    error_code=e.error_code,
                    message=e.message,
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

        while True:
            if self._h_camera is None:
                try:
                    self.open()
                except RuntimeError:
                    logger.warning("mindvision_stream_waiting")
                    time.sleep(2)
                    continue

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

    def camera_info(self) -> dict:
        if self._h_camera is None or self._dev_info is None:
            return {"type": "mindvision", "status": "closed"}
        return {
            "type": "mindvision",
            "model": self._dev_info.GetFriendlyName(),
            "port_type": self._dev_info.GetPortType(),
            "mono": self._mono,
            "max_width": self._cap.sResolutionRange.iWidthMax,
            "max_height": self._cap.sResolutionRange.iHeightMax,
        }
