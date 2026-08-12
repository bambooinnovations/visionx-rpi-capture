from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import structlog

import config
from camera.base import BaseCamera
from metrics import CaptureMetrics

logger = structlog.get_logger()

_DEFAULT_SIZE = (1280, 960)


class MockCamera(BaseCamera):
    """Software-only stand-in for local development without a Pi/MindVision.

    Uses the machine's own webcam (device index 0) if one is available;
    otherwise falls back to a synthetic generated frame so the rest of the
    app (streaming, capture, stitching) still has something to work with.
    """

    def __init__(self, camera_index: int = 0) -> None:
        self.camera_index = camera_index
        self._cap: cv2.VideoCapture | None = None
        self._lock = threading.Lock()
        self._using_webcam = False
        self._open_attempted = False
        self._stream_size: tuple[int, int] = _DEFAULT_SIZE

    def open(self) -> None:
        if self._open_attempted:
            return
        self._open_attempted = True

        cap = cv2.VideoCapture(self.camera_index)
        if cap.isOpened():
            self._cap = cap
            self._using_webcam = True
            logger.info("mock_camera_using_webcam", device_index=self.camera_index)
        else:
            cap.release()
            self._cap = None
            self._using_webcam = False
            logger.info("mock_camera_using_synthetic_frames", reason="no_webcam_available")

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._using_webcam = False
        self._open_attempted = False

    def _grab_frame(self, size: tuple[int, int]) -> "np.ndarray":
        if self._using_webcam and self._cap is not None:
            with self._lock:
                ok, frame = self._cap.read()
            if ok:
                return cv2.resize(frame, size)
            logger.warning("mock_camera_webcam_read_failed")
        return _synthetic_frame(size)

    def stream_frames(
        self,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
    ):
        self.open()

        frame_interval = 1.0 / (fps if fps is not None else config.STREAM_FPS)
        size = (width or self._stream_size[0], height or self._stream_size[1])

        while True:
            start = time.monotonic()
            frame = self._grab_frame(size)
            ok, encoded = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.STREAM_QUALITY]
            )
            if ok:
                yield encoded.tobytes()

            elapsed = time.monotonic() - start
            remaining = frame_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def capture_image(
        self,
        resolution: tuple[int, int] | None = None,
        output_folder: Path = config.CAPTURE_TMP_DIR,
    ) -> tuple[Path, CaptureMetrics]:
        self.open()

        resolution = resolution or _DEFAULT_SIZE
        captured_at = datetime.now(timezone.utc).isoformat()
        output_folder.mkdir(parents=True, exist_ok=True)
        output_image = output_folder / f"{int(time.time())}.jpg"

        t0 = time.perf_counter()
        frame = self._grab_frame(resolution)
        cv2.imwrite(str(output_image), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        capture_duration_ms = (time.perf_counter() - t0) * 1000

        metrics = CaptureMetrics(
            captured_at=captured_at,
            capture_duration_ms=capture_duration_ms,
            width=resolution[0],
            height=resolution[1],
            file_size_bytes=output_image.stat().st_size,
        )
        return output_image, metrics

    def camera_info(self) -> dict:
        return {
            "type": "mock",
            "source": "webcam" if self._using_webcam else "synthetic",
            "stream_size": self._stream_size,
        }


def _synthetic_frame(size: tuple[int, int]) -> "np.ndarray":
    """Deterministic-ish test pattern with a live timestamp, used when no
    device webcam is available."""
    width, height = size
    frame = np.zeros((height, width, 3), dtype=np.uint8)

    bar_width = max(width // 8, 1)
    colors = [
        (255, 255, 255), (0, 255, 255), (255, 255, 0), (0, 255, 0),
        (255, 0, 255), (0, 0, 255), (255, 0, 0), (0, 0, 0),
    ]
    for i, color in enumerate(colors):
        x0 = i * bar_width
        frame[:, x0:x0 + bar_width] = color

    text = datetime.now().strftime("MOCK CAMERA  %H:%M:%S.%f")[:-3]
    cv2.putText(
        frame, text, (20, height - 30),
        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA,
    )
    cv2.putText(
        frame, text, (20, height - 30),
        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA,
    )
    return frame
