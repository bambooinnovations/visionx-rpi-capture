import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

METRICS_DB_PATH = Path(os.environ.get("METRICS_DB_PATH", "/data/visionx_metrics.db"))


@dataclass
class CaptureMetrics:
    captured_at: str  # ISO 8601 UTC timestamp
    capture_duration_ms: float
    width: int
    height: int
    file_size_bytes: int


def init_db(db_path: Path = METRICS_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS capture_metrics (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at         TEXT    NOT NULL,
                capture_duration_ms REAL    NOT NULL,
                width               INTEGER NOT NULL,
                height              INTEGER NOT NULL,
                file_size_bytes     INTEGER NOT NULL
            )
        """)


def get_stats(db_path: Path = METRICS_DB_PATH) -> dict:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("""
            SELECT
                COUNT(*),
                AVG(capture_duration_ms), MIN(capture_duration_ms), MAX(capture_duration_ms),
                AVG(file_size_bytes),     MIN(file_size_bytes),     MAX(file_size_bytes)
            FROM capture_metrics
        """).fetchone()

    def r(v):
        return round(v, 2) if v is not None else None

    return {
        "total_captures": row[0],
        "capture_duration_ms": {"avg": r(row[1]), "min": r(row[2]), "max": r(row[3])},
        "file_size_bytes": {"avg": r(row[4]), "min": row[5], "max": row[6]},
    }


def record_capture(metrics: CaptureMetrics, db_path: Path = METRICS_DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO capture_metrics (captured_at, capture_duration_ms, width, height, file_size_bytes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                metrics.captured_at,
                metrics.capture_duration_ms,
                metrics.width,
                metrics.height,
                metrics.file_size_bytes,
            ),
        )
