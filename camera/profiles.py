"""SQLite store for named capture profiles.

A profile is a named, complete copy of the MindVision settings the settings
page edits (every key in camera.mindvision.SETTING_KEYS), scoped to one camera
serial number. VisionX
references a profile by its UUID `id`; the RPi owns the data.

`active_profile` records which profile was last applied to each camera so it
can be re-applied after a restart or USB reconnect.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config
from camera.mindvision import SETTING_KEYS

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_profile (
    id            TEXT PRIMARY KEY,
    camera_serial TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    settings      TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (camera_serial, name)
);
CREATE TABLE IF NOT EXISTS active_profile (
    camera_serial TEXT PRIMARY KEY,
    profile_id    TEXT REFERENCES capture_profile(id) ON DELETE SET NULL,
    applied_at    TEXT NOT NULL
);
"""


class ProfileError(Exception):
    """Base class; `status` is the HTTP status the API should answer with."""
    status = 400


class ProfileNotFound(ProfileError):
    status = 404


class ProfileConflict(ProfileError):
    status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_settings(settings) -> dict:
    """Return a clean settings dict, or raise ProfileError.

    A profile is a complete copy of the settings the settings page edits: every
    key in SETTING_KEYS, no more, no fewer. Values must be plain scalars; range
    checks happen when the profile is applied (camera.mindvision.apply_settings
    knows the hardware).
    """
    if not isinstance(settings, dict):
        raise ProfileError("settings must be an object")
    unknown = sorted(k for k in settings if k not in SETTING_KEYS)
    if unknown:
        raise ProfileError(f"unknown setting(s): {', '.join(unknown)}")
    missing = [k for k in SETTING_KEYS if k not in settings]
    if missing:
        raise ProfileError(f"missing setting(s): {', '.join(missing)}")
    for key, value in settings.items():
        if not isinstance(value, (bool, int, float, str)):
            raise ProfileError(f"setting '{key}' must be a number, boolean or string")
    return dict(settings)


def _clean_name(name) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("name is required")
    name = name.strip()
    if len(name) > 100:
        raise ProfileError("name must be 100 characters or fewer")
    return name


def _clean_description(description) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise ProfileError("description must be a string")
    return description.strip()[:1000]


def _row_to_profile(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "camera_serial": row["camera_serial"],
        "name": row["name"],
        "description": row["description"],
        "settings": json.loads(row["settings"]),
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class ProfileStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._path = Path(db_path or config.PROFILES_DB_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _connect(self):
        """One short-lived connection per operation; commits on success."""
        with closing(sqlite3.connect(self._path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn

    # ── Profiles ─────────────────────────────────────────────────────────────

    def list(self, serial: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM capture_profile WHERE camera_serial = ? ORDER BY name COLLATE NOCASE",
                (serial,),
            ).fetchall()
        return [_row_to_profile(r) for r in rows]

    def get(self, serial: str, profile_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM capture_profile WHERE id = ? AND camera_serial = ?",
                (profile_id, serial),
            ).fetchone()
        if row is None:
            raise ProfileNotFound(f"Profile {profile_id} not found")
        return _row_to_profile(row)

    def find(self, profile_id: str) -> dict:
        """Look a profile up by id alone (ids are globally unique UUIDs)."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM capture_profile WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise ProfileNotFound(f"Profile {profile_id} not found")
        return _row_to_profile(row)

    def _insert(self, serial: str, profile_id: str, name: str, description: str, settings: dict) -> None:
        now = _now()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO capture_profile "
                    "(id, camera_serial, name, description, settings, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (profile_id, serial, name, description, json.dumps(settings), now, now),
                )
        except sqlite3.IntegrityError:
            raise ProfileConflict(f"A profile named '{name}' already exists") from None

    def create(self, serial: str, name, settings, description=None) -> dict:
        name = _clean_name(name)
        settings = validate_settings(settings)
        description = _clean_description(description)
        profile_id = str(uuid.uuid4())
        self._insert(serial, profile_id, name, description, settings)
        return self.get(serial, profile_id)

    def update(self, serial: str, profile_id: str, *, name=None, description=None, settings=None) -> dict:
        """Change any of name / description / settings; bumps `revision`."""
        current = self.get(serial, profile_id)
        new_name = _clean_name(name) if name is not None else current["name"]
        new_desc = _clean_description(description) if description is not None else current["description"]
        new_settings = validate_settings(settings) if settings is not None else current["settings"]
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE capture_profile SET name = ?, description = ?, settings = ?, "
                    "revision = revision + 1, updated_at = ? WHERE id = ? AND camera_serial = ?",
                    (new_name, new_desc, json.dumps(new_settings), _now(), profile_id, serial),
                )
        except sqlite3.IntegrityError:
            raise ProfileConflict(f"A profile named '{new_name}' already exists") from None
        return self.get(serial, profile_id)

    def delete(self, serial: str, profile_id: str) -> None:
        """Delete a profile. If it was active, the active pointer is cleared
        (ON DELETE SET NULL); the camera's current settings are untouched."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM capture_profile WHERE id = ? AND camera_serial = ?",
                (profile_id, serial),
            )
        if cur.rowcount == 0:
            raise ProfileNotFound(f"Profile {profile_id} not found")

    # ── Active profile ───────────────────────────────────────────────────────

    def get_active(self, serial: str) -> dict | None:
        """{"profile": <profile>, "applied_at": str}, or None if none is active."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT profile_id, applied_at FROM active_profile WHERE camera_serial = ?",
                (serial,),
            ).fetchone()
        if row is None or row["profile_id"] is None:
            return None
        return {"profile": self.get(serial, row["profile_id"]), "applied_at": row["applied_at"]}

    def set_active(self, serial: str, profile_id: str) -> None:
        self.get(serial, profile_id)  # 404 if it isn't this camera's profile
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO active_profile (camera_serial, profile_id, applied_at) VALUES (?, ?, ?) "
                "ON CONFLICT(camera_serial) DO UPDATE SET "
                "profile_id = excluded.profile_id, applied_at = excluded.applied_at",
                (serial, profile_id, _now()),
            )

    # ── Backup / transfer ────────────────────────────────────────────────────

    def import_profiles(self, serial: str, profiles) -> dict:
        """Upsert profiles by id for this camera.

        Returns {"imported": n, "errors": {id_or_index: message}}. An existing
        id is overwritten (revision bumped); a name clash with a different
        profile skips that entry.
        """
        if not isinstance(profiles, list):
            raise ProfileError("profiles must be a list")
        imported, errors = 0, {}
        for i, item in enumerate(profiles):
            key = str(item.get("id", i)) if isinstance(item, dict) else str(i)
            try:
                if not isinstance(item, dict):
                    raise ProfileError("entry must be an object")
                pid = item.get("id")
                if not isinstance(pid, str) or not pid:
                    raise ProfileError("id is required")
                try:
                    self.get(serial, pid)
                except ProfileNotFound:
                    self._insert(
                        serial, pid, _clean_name(item.get("name")),
                        _clean_description(item.get("description")),
                        validate_settings(item.get("settings")),
                    )
                else:
                    self.update(
                        serial, pid, name=item.get("name"),
                        description=item.get("description"), settings=item.get("settings"),
                    )
                imported += 1
            except ProfileError as exc:
                errors[key] = str(exc)
        return {"imported": imported, "errors": errors}


_store: ProfileStore | None = None


def get_store() -> ProfileStore:
    """Process-wide store, created on first use."""
    global _store
    if _store is None:
        _store = ProfileStore()
    return _store
