"""Runtime configuration overrides.

Values here take precedence over configuration.toml and persist across
restarts via runtime_config.json (gitignored).  Updated via PATCH /rpi/config.
"""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).parent / "runtime_config.json"

# Keys that can be changed at runtime without restarting the service.
# Maps dotted "section.key" → expected Python type.
UPDATABLE: dict[str, type] = {
    "stream.fps":                     int,
    "stream.quality":                 int,
    "hw_trigger.destination_url":     str,
    "hw_trigger.destination_api_key": str,
    "hw_trigger.retry_attempts":      int,
    "hw_trigger.timeout_seconds":     int,
    "hw_trigger.save_local":          bool,
    "hw_trigger.local_max_files":     int,
    "hw_trigger.local_max_mb":        int,
    "hw_trigger.raw_destination_url":      str,
    "hw_trigger.send_raw_images":          bool,
    "hw_trigger.use_stitch":               bool,
    "hw_trigger.trigger_queue_maxsize":    int,
    "hw_trigger.stitch_memory_budget_mb":  int,
    "hw_trigger.raw_memory_budget_mb":     int,
    "hw_trigger.max_queue_age_s":          float,
}

# Values that are masked ("***") in GET /rpi/config responses.
MASKED_KEYS: frozenset[str] = frozenset({"hw_trigger.destination_api_key"})


def load() -> dict:
    """Return all current runtime overrides."""
    try:
        return json.loads(PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get(key: str, default=None):
    """Return the runtime override for key, or default if not set."""
    return load().get(key, default)


def update(key: str, value) -> None:
    """Set a runtime override and persist it."""
    data = load()
    data[key] = value
    PATH.write_text(json.dumps(data, indent=2))


def delete(key: str) -> bool:
    """Remove a runtime override.  Returns True if the key existed."""
    data = load()
    existed = key in data
    data.pop(key, None)
    PATH.write_text(json.dumps(data, indent=2))
    return existed
