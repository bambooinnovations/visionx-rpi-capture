"""Runtime calibration store.

Reads and writes calibration.json next to this file.  Each section (e.g.
"white_balance") is a flat dict of measured values plus a "calibrated_at"
timestamp.  The file is gitignored — it lives on the device that ran the
calibration.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CALIBRATION_PATH = Path(__file__).parent / "calibration.json"


def load() -> dict:
    """Return the full calibration dict, or {} if the file doesn't exist yet."""
    try:
        return json.loads(CALIBRATION_PATH.read_text())
    except FileNotFoundError:
        return {}


def save(section: str, values: dict) -> dict:
    """Merge *values* into *section* and persist.  Returns the updated full dict."""
    data = load()
    data[section] = {**values, "calibrated_at": datetime.now(timezone.utc).isoformat()}
    CALIBRATION_PATH.write_text(json.dumps(data, indent=2))
    return data
