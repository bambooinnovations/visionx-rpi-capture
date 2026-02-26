from __future__ import annotations

import io
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

import config
from metrics import CaptureMetrics

logger = structlog.get_logger()

_camera: "Picamera2 | None" = None
# Held by stream_frames() during each frame grab, and by capture_image() for the
# full AF + mode-switch + capture cycle.  This ensures the two never overlap.
_camera_lock = threading.Lock()
# Cached so capture_image() can restore preview mode without re-creating the dict.
_preview_config: dict | None = None
# Resolved during _ensure_camera() from: camera profile → auto-detect.
_capture_size: tuple[int, int] | None = None
_stream_size: tuple[int, int] | None = None


def init_camera() -> None:
    """Warm up the camera and log the resolved configuration."""
    _ensure_camera()
    logger.info(
        "camera_initialized",
        stream_size=_stream_size,
        capture_size=_capture_size,
    )


def _select_sensor_modes(
    cam: "Picamera2",
    stream_size: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return (capture_size, raw_preview_size) for the connected camera.

    capture_size priority:  camera profile → auto-detect (largest full-sensor mode)
    raw_preview_size:       always auto-detected — fastest full-sensor mode covering
                            stream_size, included in the preview config to pin
                            libcamera to the full field of view.
    """
    modes = cam.sensor_modes
    model = cam.camera_properties.get("Model", "")

    # crop_limits format: (x_offset, y_offset, width, height)
    full_sensor = [
        m for m in modes
        if m.get("crop_limits", (1,))[0] == 0
        and m.get("crop_limits", (0, 1))[1] == 0
    ]
    if not full_sensor:
        full_sensor = modes  # camera doesn't report crop_limits — use all modes

    profile = config.get_camera_profile(model)

    # --- capture_size ---
    if "capture_size" in profile:
        w, h = profile["capture_size"]
        capture_size = (int(w), int(h))
        logger.info("capture_size_source", source="camera_profile", model=model, size=capture_size)
    else:
        largest = max(full_sensor, key=lambda m: m["size"][0] * m["size"][1])
        capture_size = largest["size"]
        logger.info("capture_size_source", source="auto_detected", model=model, size=capture_size)

    # --- raw_preview_size: fastest full-sensor mode covering stream_size ---
    covering = [
        m for m in full_sensor
        if m["size"][0] >= stream_size[0] and m["size"][1] >= stream_size[1]
    ]
    if not covering:
        covering = full_sensor
    fastest = max(covering, key=lambda m: m.get("fps", 0))
    raw_preview_size = fastest["size"]

    return capture_size, raw_preview_size


def _ensure_camera() -> "Picamera2":
    """Return the singleton Picamera2 instance, creating it if necessary.

    The camera is always left running in preview configuration with the main
    stream at the resolved stream_size so that stream_frames() can read
    continuously.  capture_image() temporarily switches to still mode and
    then restores this config.
    """
    if not _PICAMERA2_AVAILABLE:
        raise RuntimeError(
            "picamera2 is not installed. On Raspberry Pi run: uv sync --extra rpi"
        )

    global _camera, _preview_config, _capture_size, _stream_size

    if _camera is not None:
        return _camera

    cam = Picamera2()

    # Resolve stream size: camera profile → default (1280x960).
    model = cam.camera_properties.get("Model", "")
    profile = config.get_camera_profile(model)
    if "stream_size" in profile:
        w, h = profile["stream_size"]
        effective_stream_size = (int(w), int(h))
        logger.info("stream_size_source", source="camera_profile", model=model, size=effective_stream_size)
    else:
        effective_stream_size = (1280, 960)
        logger.info("stream_size_source", source="default", model=model, size=effective_stream_size)

    _stream_size = effective_stream_size

    capture_size, raw_preview_size = _select_sensor_modes(cam, effective_stream_size)
    _capture_size = capture_size

    logger.info(
        "sensor_modes_selected",
        capture_size=capture_size,
        raw_preview_size=raw_preview_size,
    )

    # raw stream pins libcamera to the full-FOV sensor mode.
    _preview_config = cam.create_preview_configuration(
        main={"size": effective_stream_size},
        raw={"size": raw_preview_size},
    )
    cam.configure(_preview_config)
    cam.options["quality"] = 95
    cam.start()

    if config.LOCK_EXPOSURE:
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

    # Continuous AF so the preview stream stays in focus.
    if "AfMode" in cam.camera_controls:
        cam.set_controls({"AfMode": 2})

    _camera = cam
    return _camera


def stream_frames():
    """Yield raw JPEG bytes for each preview frame.

    Acquires _camera_lock around each frame grab so that capture_image() can
    take over the camera without racing.  The lock is released before yielding
    so the HTTP layer can flush the frame to the client independently.
    """
    cam = _ensure_camera()
    frame_interval = 1.0 / config.STREAM_FPS

    while True:
        start = time.monotonic()
        frame_data = None

        with _camera_lock:
            try:
                img = cam.capture_image("main")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=config.STREAM_QUALITY)
                frame_data = buf.getvalue()
            except Exception:
                logger.exception("stream_frame_skipped")

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
    resolution: tuple[int, int] | None = None,
    output_folder: Path = config.CAPTURE_TMP_DIR,
) -> tuple[Path, CaptureMetrics]:
    """Capture a high-quality still image.

    resolution — (width, height) to capture at.  Defaults to the size resolved
                 from camera_profiles in configuration.toml, or auto-detected
                 at camera init.

    Workflow:
      1. Acquire _camera_lock (stream_frames() releases it between frames).
      2. Run autofocus in preview mode — more frames available than still mode.
      3. Stop camera, reconfigure to still mode, restart.
      4. Capture to file.
      5. Stop camera, restore preview config, restart.
      6. Release lock so streaming resumes.
    """
    cam = _ensure_camera()

    if resolution is None:
        resolution = _capture_size or (4624, 3472)

    captured_at = datetime.now(timezone.utc).isoformat()
    output_image = output_folder / f"{int(time.time())}.jpg"

    still_config = cam.create_still_configuration(
        main={"size": resolution},
        controls={
            "Sharpness": config.CAMERA_SHARPNESS,
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

        # Restore preview mode so streaming can resume.
        cam.stop()
        cam.configure(_preview_config)
        cam.start()
        if "AfMode" in cam.camera_controls:
            cam.set_controls({"AfMode": 2})

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
