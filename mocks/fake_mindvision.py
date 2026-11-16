"""Fake MindVision camera: no SDK, no hardware, but the real capture-side code paths.

FakeMindVisionCamera subclasses MindVisionCamera, so the app registers it as a MindVision camera and the whole
hardware-trigger pipeline (decoder routes, SerialTriggerListener, per-camera workers, uploads, monitor page) runs
unchanged. Only the SDK-facing methods are replaced. capture_image() builds a synthetic fabric frame and then runs
the *real* EXIF builder and JPEG encoder, so the EXIF (including the trigger distance) is produced by production code.

Enable with `[camera] type = "mock_mindvision"` (and `mock_count = 2`) in the config; see mocks/e2e.py.

Scope: the hardware-trigger / capture path. The settings, lens and exposure pages talk to the SDK directly and are
not simulated.

Fault injection (for tests): fail_next_captures, capture_delay_s.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

import config
from camera.mindvision import CameraMode, MindVisionCamera
from metrics import CaptureMetrics


class _FakeDevInfo:
    def __init__(self, index: int) -> None:
        self._index = index

    def GetSn(self) -> str:
        return f"MOCK-{self._index:04d}"

    def GetFriendlyName(self) -> str:
        return f"Mock MindVision {self._index}"

    def GetProductName(self) -> str:
        return "MOCK-MV-CA050"

    def GetPortType(self) -> str:
        return "mock"


class FakeMindVisionCamera(MindVisionCamera):
    def __init__(self, camera_index: int = 0, frame_size: tuple[int, int] = (1224, 1024)) -> None:
        super().__init__(camera_index)
        self._frame_size = frame_size
        self._rng = np.random.default_rng(1000 + camera_index)
        self._texture = self._make_texture()
        self._capture_count = 0
        # fault injection
        self.fail_next_captures = 0
        self.capture_delay_s = 0.0
        self._fault_lock = threading.Lock()

    # ── lifecycle (SDK replaced) ─────────────────────────────────────────
    def open(self) -> None:
        self._dev_info = _FakeDevInfo(self._camera_index)
        self._h_camera = 1  # truthy sentinel: "open"

    def close(self) -> None:
        self._h_camera = None

    def set_trigger_mode(self, mode: int) -> None:
        if self._h_camera is None:
            raise RuntimeError("Camera not open")

    def set_mode(self, mode: CameraMode) -> None:
        if self._h_camera is None:
            raise RuntimeError("Camera not open")
        self._mode = mode

    def discard_draft(self) -> list[str]:
        return []

    def begin_stream_exposure(self) -> None:
        pass

    def end_stream_exposure(self) -> None:
        pass

    def camera_info(self) -> dict:
        if self._h_camera is None:
            return {"type": "mindvision", "camera_id": self._camera_index, "status": "closed"}
        w, h = self._frame_size
        return {
            "type": "mindvision", "camera_id": self._camera_index, "mock": True,
            "serial_number": self._dev_info.GetSn(), "model": self._dev_info.GetFriendlyName(),
            "product_name": self._dev_info.GetProductName(), "port_type": "mock", "mono": False,
            "max_width": w, "max_height": h, "capture_size": None, "stream_size": None,
        }

    # ── frames ───────────────────────────────────────────────────────────
    def _make_texture(self) -> np.ndarray:
        """A woven-looking pattern, distinct per camera, so frames are plausible and cameras are tellable apart."""
        w, h = self._frame_size
        yy, xx = np.mgrid[0:h, 0:w]
        weave = ((xx // 6 + yy // 6) % 2) * 30 + 90
        base = np.zeros((h, w, 3), np.uint8)
        hue = (self._camera_index * 60) % 180
        base[:] = cv2.cvtColor(np.uint8([[[hue, 60, 140]]]), cv2.COLOR_HSV2BGR)[0, 0]
        img = np.clip(base.astype(np.int16) + weave[..., None] - 110, 0, 255).astype(np.uint8)
        noise = self._rng.integers(-6, 7, img.shape, dtype=np.int16)
        return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    def _render_frame(self, extra: dict | None) -> np.ndarray:
        frame = self._texture.copy()
        w, h = self._frame_size
        lines = [f"MOCK CAM {self._camera_index}  #{self._capture_count}"]
        if extra:
            if extra.get("trigger_number") is not None:
                lines.append(f"trigger {extra['trigger_number']}")
            if extra.get("distance_mm") is not None:
                lines.append(f"distance {extra['distance_mm'] / 1000:.3f} m")
        for i, text in enumerate(lines):
            cv2.putText(frame, text, (30, 70 + i * 70), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 8)
            cv2.putText(frame, text, (30, 70 + i * 70), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 3)
        return frame

    def capture_image(
        self,
        resolution: tuple[int, int] | None = None,
        output_folder: Path = config.CAPTURE_TMP_DIR,
        use_draft: bool = False,
        extra_exif: dict | None = None,
    ) -> tuple[Path, CaptureMetrics]:
        if self._h_camera is None:
            self.open()
        with self._fault_lock:
            fail = self.fail_next_captures > 0
            if fail:
                self.fail_next_captures -= 1
        t0 = time.perf_counter()
        if self.capture_delay_s:
            time.sleep(self.capture_delay_s)
        if fail:
            raise RuntimeError("Failed to capture frame from MindVision camera (injected fault)")

        captured_at = datetime.now(timezone.utc).isoformat()
        self._capture_count += 1
        frame = self._render_frame(extra_exif)
        resize = resolution if resolution is not None and resolution != self._frame_size else None
        state = {"exposure_us": 8000, "ae_enabled": False, "analog_gain_raw": 16, "mock": True}

        # Real EXIF builder + JPEG encoder, so the embedded metadata comes from production code.
        exif_bytes = self._build_exif(captured_at, state, extra_exif)
        jpeg = self._encode_jpeg(frame, quality=90, exif_bytes=exif_bytes, resize=resize)
        output_folder.mkdir(parents=True, exist_ok=True)
        path = output_folder / f"cam{self._camera_index}_{time.time_ns()}.jpg"
        path.write_bytes(jpeg)

        width, height = resize or self._frame_size
        return path, CaptureMetrics(
            captured_at=captured_at,
            capture_duration_ms=(time.perf_counter() - t0) * 1000,
            width=width, height=height, file_size_bytes=path.stat().st_size,
        )

    def stream_frames(self, width: int | None = None, height: int | None = None, fps: float | None = None):
        interval = 1.0 / (fps if fps is not None else config.STREAM_FPS)
        self._stream_count += 1
        self._streaming = True
        self._stream_cancel.clear()
        try:
            while not self._stream_cancel.is_set():
                frame = self._render_frame(None)
                size = (width, height) if width and height else None
                yield self._encode_jpeg(frame, config.STREAM_QUALITY, resize=size)
                time.sleep(interval)
        finally:
            self._stream_count -= 1
            self._streaming = self._stream_count > 0
