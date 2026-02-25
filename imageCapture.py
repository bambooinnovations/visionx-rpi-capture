from __future__ import annotations

import io
import os
import threading
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

# Stream resolution for the lores preview channel.
# Width must be a multiple of 32, height a multiple of 2 (libcamera alignment).
STREAM_SIZE = (
    int(os.environ.get("STREAM_WIDTH", "1280")),
    int(os.environ.get("STREAM_HEIGHT", "960")),
)
STREAM_FPS = int(os.environ.get("STREAM_FPS", "15"))
STREAM_QUALITY = int(os.environ.get("STREAM_QUALITY", "60"))

_SHARPNESS = float(os.environ.get("CAMERA_SHARPNESS", "1.0"))
_LOCK_EXPOSURE = os.environ.get("LOCK_EXPOSURE", "false").lower() == "true"
DEFAULT_CAPTURE_TMP_DIR = Path(
    os.environ.get("CAPTURE_TMP_DIR", "/tmp/visionx_captures")
)

_camera: "Picamera2 | None" = None
# Held by stream_frames() during each frame grab, and by capture_image() for the
# full AF + mode-switch + capture cycle.  This ensures the two never overlap.
_camera_lock = threading.Lock()
# Cached so capture_image() can restore preview mode without re-creating the dict.
_preview_config: dict | None = None


def init_camera() -> None:
    """Warm up the camera in preview+lores mode at startup."""
    _ensure_camera()
    logger.info(
        "camera_initialized",
        stream_size=STREAM_SIZE,
        capture_size=DEFAULT_IMAGE_SIZE,
    )


def _ensure_camera() -> "Picamera2":
    """Return the singleton Picamera2 instance, creating it if necessary.

    The camera is always left running in preview configuration with a lores
    stream so that stream_frames() can read continuously.  capture_image()
    temporarily switches to still mode and then restores this config.
    """
    if not _PICAMERA2_AVAILABLE:
        raise RuntimeError(
            "picamera2 is not installed. On Raspberry Pi run: uv sync --extra rpi"
        )

    global _camera, _preview_config

    if _camera is not None:
        return _camera

    cam = Picamera2()

    _preview_config = cam.create_preview_configuration(
        main={"size": DEFAULT_IMAGE_SIZE},
        lores={"size": STREAM_SIZE},
    )
    cam.configure(_preview_config)
    cam.options["quality"] = 95
    cam.start()

    if _LOCK_EXPOSURE:
        time.sleep(2)  # let AE/AWB converge under the rig lighting
        meta = cam.capture_metadata()
        cam.set_controls({
            "AeEnable":     False,
            "AwbEnable":    False,
            "ExposureTime": meta["ExposureTime"],
            "AnalogueGain": meta["AnalogueGain"],
            "ColourGains":  meta["ColourGains"],
        })
        logger.info(
            "exposure_locked",
            exposure_us=meta["ExposureTime"],
            gain=round(meta["AnalogueGain"], 2),
        )

    # Single-shot AF for still captures.  Continuous AF (AfMode=2) requires a
    # fast preview stream to run its algorithm — which we now have.
    if "AfMode" in cam.camera_controls:
        cam.set_controls({"AfMode": 1})

    _camera = cam
    return _camera


def stream_frames():
    """Yield raw JPEG bytes for each lores preview frame.

    Acquires _camera_lock around each frame grab so that capture_image() can
    take over the camera without racing.  The lock is released before yielding
    so the HTTP layer can flush the frame to the client independently.
    """
    cam = _ensure_camera()
    frame_interval = 1.0 / STREAM_FPS

    while True:
        start = time.monotonic()
        frame_data = None

        with _camera_lock:
            try:
                img = cam.capture_image("lores")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=STREAM_QUALITY)
                frame_data = buf.getvalue()
            except Exception:
                logger.warning("stream_frame_skipped")

        if frame_data:
            yield frame_data

        elapsed = time.monotonic() - start
        remaining = frame_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)


def _laplacian_score(path: str) -> float:
    """Laplacian variance — proxy for optical sharpness. Higher = sharper."""
    import numpy as np
    from PIL import Image
    arr = np.array(Image.open(path).convert("L"), dtype=np.float64)
    lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] +
           arr[1:-1, :-2] + arr[1:-1, 2:] - 4 * arr[1:-1, 1:-1])
    return round(float(lap.var()), 2)


def capture_image(
    resolution: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    output_folder: Path = DEFAULT_CAPTURE_TMP_DIR,
) -> tuple[Path, CaptureMetrics]:
    """Capture a high-quality still image.

    Workflow:
      1. Acquire _camera_lock (stream_frames() releases it between frames).
      2. Run autofocus in preview mode — more frames available than still mode.
      3. Stop camera, reconfigure to still mode, restart.
      4. Capture to file.
      5. Stop camera, restore preview+lores config, restart.
      6. Release lock so streaming resumes.
    """
    cam = _ensure_camera()
    captured_at = datetime.now(timezone.utc).isoformat()
    output_image = output_folder / f"{int(time.time())}.jpg"

    still_config = cam.create_still_configuration(
        main={"size": resolution},
        controls={
            "Sharpness": _SHARPNESS,
            "NoiseReductionMode": 2,
        },
    )

    with _camera_lock:
        # AF while still in preview mode — lens converges faster with more frames.
        if "AfMode" in cam.camera_controls:
            success = cam.autofocus_cycle()
            if not success:
                logger.warning("autofocus_failed", path=str(output_image))

        # Switch to high-quality still mode.
        cam.stop()
        cam.configure(still_config)
        cam.start()

        t0 = time.perf_counter()
        cam.capture_file(str(output_image))
        capture_duration_ms = (time.perf_counter() - t0) * 1000

        # Restore preview+lores mode so streaming can resume.
        cam.stop()
        cam.configure(_preview_config)
        cam.start()
        if "AfMode" in cam.camera_controls:
            cam.set_controls({"AfMode": 1})

    sharpness = _laplacian_score(str(output_image))
    logger.info("capture_sharpness", score=sharpness, path=str(output_image))

    metrics = CaptureMetrics(
        captured_at=captured_at,
        capture_duration_ms=capture_duration_ms,
        width=resolution[0],
        height=resolution[1],
        file_size_bytes=output_image.stat().st_size,
    )

    return output_image, metrics
