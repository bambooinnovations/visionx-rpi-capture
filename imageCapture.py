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

_SHARPNESS = float(os.environ.get("CAMERA_SHARPNESS", "1.0"))
_LOCK_EXPOSURE = os.environ.get("LOCK_EXPOSURE", "false").lower() == "true"
DEFAULT_CAPTURE_TMP_DIR = Path(
    os.environ.get("CAPTURE_TMP_DIR", "/tmp/visionx_captures")
)

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
    config = cam.create_still_configuration(
        main={"size": resolution},
        controls={
            # Match rpicam-still defaults for maximum sharpness.
            # Sharpness=1.0 activates the IPA's sharpening algorithm.
            # Setting it to 0 (as Arducam docs sometimes show) drops Laplacian
            # score by ~15% and is the #1 cause of "params make it softer".
            "Sharpness": _SHARPNESS,
            # HighQuality NR for stills (rpicam-still default for still capture).
            "NoiseReductionMode": 2,
        },
    )
    cam.configure(config)
    cam.options["quality"] = 95  # JPEG quality; picamera2 default is lower
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
        logger.info("exposure_locked",
                    exposure_us=meta["ExposureTime"],
                    gain=round(meta["AnalogueGain"], 2))

    # Single-shot AF (AfMode=1) is correct for still capture.
    # Continuous AF (AfMode=2) needs a fast preview stream to drive its algorithm;
    # a still configuration doesn't supply enough frames, so the lens never moves.
    # NEVER use AfMode=0 + LensPosition=0.0 unless shooting at true infinity —
    # that is the biggest cause of soft picamera2 images.
    if "AfMode" in cam.camera_controls:
        cam.set_controls({"AfMode": 1})

    _camera = cam
    _camera_resolution = resolution
    return _camera


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
    """Capture an image using picamera2 at the requested resolution.

    The camera is kept warm across requests — no subprocess spawn or
    AGC/AWB settling delay on repeated calls.
    For the ArduCam 64MP, (4624, 3472) uses the full sensor via 2x2 binning.
    """
    captured_at = datetime.now(timezone.utc).isoformat()
    output_image = output_folder / f"{int(time.time())}.jpg"

    camera = _ensure_camera(resolution)

    if "AfMode" in camera.camera_controls:
        success = camera.autofocus_cycle()
        if not success:
            logger.warning("autofocus_failed", path=str(output_image))

    t0 = time.perf_counter()
    camera.capture_file(str(output_image))
    capture_duration_ms = (time.perf_counter() - t0) * 1000

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
