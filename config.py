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
CAMERA_SHARPNESS: float = _get("camera", "sharpness", 1.0)
LOCK_EXPOSURE: bool = _get("camera", "lock_exposure", False)

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


def get_camera_profile(model: str) -> dict:
    """Return the camera_profiles entry for model, or {} if not listed."""
    return _cfg.get("camera_profiles", {}).get(model, {})
