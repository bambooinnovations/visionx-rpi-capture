"""Central configuration loader.

Reads configuration.toml once at import time and exposes all settings as
typed module-level constants.  Every other module imports from here instead
of calling os.environ.get() directly.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "configuration.toml"

try:
    with open(_CONFIG_PATH, "rb") as _f:
        _cfg = tomllib.load(_f)
except FileNotFoundError:
    _cfg = {}


def _get(section: str, key: str, default):
    return _cfg.get(section, {}).get(key, default)


# Server
ENV: str = _get("server", "env", "dev")

# Station (MindVision only)
# "fabric"  — production line: cameras default to hardware-trigger mode; the
#             decoder auto-starts if its serial port is present at boot.
# "qc"      — quality-control workstation: cameras default to software-trigger
#             capture mode at boot so /rpi/capture works immediately; the
#             decoder never auto-starts.
# "shading" — shade-checking workstation: identical capture behavior to "qc"
#             (software-trigger stills, no decoder / hardware trigger).
STATION_TYPE: str = _get("station", "type", "fabric")


def is_capture_station() -> bool:
    """True for on-demand capture stations (qc, shading) — as opposed to the
    fabric production line's hardware-trigger flow."""
    return STATION_TYPE in ("qc", "shading")


# Camera
# "auto"       — probe for MindVision devices and a Pi CSI camera independently
#                and register whatever is actually present (default); can mix
#                both types in one deployment.
# "picamera2"  — force Pi CSI camera only, never probe for MindVision.
# "mindvision" — force MindVision only, never probe for a Pi CSI camera.
# "mock"       — no real hardware; uses the local machine's webcam if present,
#                else a generated test-pattern frame. For local development.
CAMERA_TYPE: str = _get("camera", "type", "auto")
CAMERA_SHARPNESS: float = _get("camera", "sharpness", 1.0)
LOCK_EXPOSURE: bool = _get("camera", "lock_exposure", False)
# None = use continuous autofocus; a float value = lock to that LensPosition.
LENS_POSITION: float | None = _get("camera", "lens_position", None)


# Stream
STREAM_FPS: int = _get("stream", "fps", 15)
STREAM_QUALITY: int = _get("stream", "quality", 60)
# Ceiling applied to an explicit ?fps= request on the stitch stream endpoint.
STREAM_MAX_FPS: float = _get("stream", "max_fps", 30.0)

# Capture
CAPTURE_TMP_DIR: Path = Path(_get("capture", "tmp_dir", "/tmp/visionx_captures"))

# Metrics
METRICS_DB_PATH: Path = Path(_get("metrics", "db_path", "/tmp/visionx_metrics.db"))

# Cleanup
CLEANUP_INTERVAL_SECONDS: int = _get("cleanup", "interval_seconds", 300)
MAX_AGE_SECONDS: int = _get("cleanup", "max_age_seconds", 300)

# Hardware trigger (MindVision only)
HW_TRIGGER_SERIAL_PORT: str = _get("hw_trigger", "serial_port", "/dev/ttyACM0")
HW_TRIGGER_SERIAL_BAUD: int = _get("hw_trigger", "serial_baud", 115200)
HW_TRIGGER_DESTINATION_URL: str = _get("hw_trigger", "destination_url", "")
HW_TRIGGER_DESTINATION_API_KEY: str = _get("hw_trigger", "destination_api_key", "")
HW_TRIGGER_RETRY_ATTEMPTS: int = _get("hw_trigger", "retry_attempts", 3)
HW_TRIGGER_TIMEOUT_SECONDS: int = _get("hw_trigger", "timeout_seconds", 10)
HW_TRIGGER_SAVE_LOCAL: bool = _get("hw_trigger", "save_local", False)
HW_TRIGGER_LOCAL_SAVE_DIR: Path = Path(_get("hw_trigger", "local_save_dir", "data/hw_captures"))
HW_TRIGGER_LOCAL_MAX_FILES: int = _get("hw_trigger", "local_max_files", 200)
HW_TRIGGER_LOCAL_MAX_MB: int = _get("hw_trigger", "local_max_mb", 500)
HW_TRIGGER_HEALTH_CHECK_URL: str = _get("hw_trigger", "health_check_url", "")
HW_TRIGGER_RAW_DESTINATION_URL: str = _get("hw_trigger", "raw_destination_url", "")
HW_TRIGGER_SEND_RAW_IMAGES: bool = _get("hw_trigger", "send_raw_images", False)


def get_camera_profile(model: str) -> dict:
    """Return the camera_profiles entry for model, or {} if not listed."""
    return _cfg.get("camera_profiles", {}).get(model, {})
