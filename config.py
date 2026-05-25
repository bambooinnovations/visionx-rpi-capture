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

# Camera
CAMERA_TYPE: str = _get("camera", "type", "picamera2")
CAMERA_SHARPNESS: float = _get("camera", "sharpness", 1.0)
LOCK_EXPOSURE: bool = _get("camera", "lock_exposure", False)
# None = use continuous autofocus; a float value = lock to that LensPosition.
LENS_POSITION: float | None = _get("camera", "lens_position", None)

# MindVision-specific
MV_CAMERA_INDEX: int = _get("camera", "mv_camera_index", 0)
MV_EXPOSURE_US: int = _get("camera", "mv_exposure_us", 30_000)
MV_AUTO_EXPOSURE: bool = _get("camera", "mv_auto_exposure", False)

# Stream
STREAM_FPS: int = _get("stream", "fps", 15)
STREAM_QUALITY: int = _get("stream", "quality", 60)

# Capture
CAPTURE_TMP_DIR: Path = Path(_get("capture", "tmp_dir", "/tmp/visionx_captures"))

# Metrics
METRICS_DB_PATH: Path = Path(_get("metrics", "db_path", "/tmp/visionx_metrics.db"))

# Cleanup
CLEANUP_INTERVAL_SECONDS: int = _get("cleanup", "interval_seconds", 300)
MAX_AGE_SECONDS: int = _get("cleanup", "max_age_seconds", 300)

# Hardware trigger (MindVision only)
HW_TRIGGER_SERIAL_PORT: str = _get("hw_trigger", "serial_port", "/dev/ttyUSB0")
HW_TRIGGER_SERIAL_BAUD: int = _get("hw_trigger", "serial_baud", 115200)
HW_TRIGGER_DESTINATION_URL: str = _get("hw_trigger", "destination_url", "")
HW_TRIGGER_DESTINATION_API_KEY: str = _get("hw_trigger", "destination_api_key", "")
HW_TRIGGER_RETRY_ATTEMPTS: int = _get("hw_trigger", "retry_attempts", 3)
HW_TRIGGER_TIMEOUT_SECONDS: int = _get("hw_trigger", "timeout_seconds", 10)
HW_TRIGGER_SAVE_LOCAL: bool = _get("hw_trigger", "save_local", True)
HW_TRIGGER_LOCAL_SAVE_DIR: Path = Path(_get("hw_trigger", "local_save_dir", "data/hw_captures"))
HW_TRIGGER_LOCAL_MAX_FILES: int = _get("hw_trigger", "local_max_files", 200)
HW_TRIGGER_LOCAL_MAX_MB: int = _get("hw_trigger", "local_max_mb", 500)


def get_camera_profile(model: str) -> dict:
    """Return the camera_profiles entry for model, or {} if not listed."""
    return _cfg.get("camera_profiles", {}).get(model, {})
