from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

try:
    from picamera2 import Picamera2
    _PICAMERA2_AVAILABLE = True
except ImportError:
    Picamera2 = None  # type: ignore[assignment,misc]
    _PICAMERA2_AVAILABLE = False

from metrics import CaptureMetrics

logger = structlog.get_logger()

DEFAULT_IMAGE_SIZE = (4624, 3472)  # ~16MP, full-sensor 2x2 binned mode for ArduCam 64MP
DEFAULT_CAPTURE_TMP_DIR = Path(os.environ.get("CAPTURE_TMP_DIR", "/tmp/visionx_captures"))

_camera: Picamera2 | None = None
_camera_resolution: tuple[int, int] | None = None


def init_camera(resolution: tuple[int, int] = DEFAULT_IMAGE_SIZE) -> None:
    """Warm up the camera at startup so the first capture request has no init delay."""
    _ensure_camera(resolution)
    logger.info("camera_initialized", width=resolution[0], height=resolution[1])


def _ensure_camera(resolution: tuple[int, int]) -> "Picamera2":
    """Return the singleton Picamera2 instance, reconfiguring only if the resolution changed."""
    if not _PICAMERA2_AVAILABLE:
        raise RuntimeError(
            "picamera2 is not installed. On Raspberry Pi run: uv sync --extra rpi"
        )

    global _camera, _camera_resolution

    if _camera is not None and _camera_resolution == resolution:
        return _camera

    if _camera is not None:
        logger.info("camera_reconfiguring", new_resolution=resolution)
        _camera.stop()
        _camera.close()

    cam = Picamera2()
    config = cam.create_still_configuration(main={"size": resolution})
    cam.configure(config)
    cam.start()
    cam.set_controls({"AfMode": 2, "AfTrigger": 0})  # continuous autofocus

    _camera = cam
    _camera_resolution = resolution
    return _camera


def capture_image(
    resolution: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    output_folder: Path = DEFAULT_CAPTURE_TMP_DIR,
) -> tuple[Path, CaptureMetrics]:
    """Capture an image using picamera2 at the requested resolution.

    The camera is kept warm across requests — no subprocess spawn or
    AGC/AWB settling delay on repeated calls.
    For the ArduCam 64MP, (4624, 3472) uses the full sensor via 2x2 binning.
    """
    captured_at = datetime.now(timezone.utc).isoformat()
    output_image = output_folder / f"{int(time.time())}.jpg"

    camera = _ensure_camera(resolution)

    t0 = time.perf_counter()
    camera.capture_file(str(output_image))
    capture_duration_ms = (time.perf_counter() - t0) * 1000

    metrics = CaptureMetrics(
        captured_at=captured_at,
        capture_duration_ms=capture_duration_ms,
        width=resolution[0],
        height=resolution[1],
        file_size_bytes=output_image.stat().st_size,
    )

    return output_image, metrics
