import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

METRICS_DB_PATH = Path(os.environ.get("METRICS_DB_PATH", "/data/visionx_metrics.db"))


@dataclass
class CaptureMetrics:
    captured_at: str           # ISO 8601 UTC timestamp
    capture_duration_ms: float
    downscale_duration_ms: float
    capture_width: int
    capture_height: int
    target_width: int
    target_height: int
    before_size_bytes: int
    after_size_bytes: int


def init_db(db_path: Path = METRICS_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS capture_metrics (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at           TEXT    NOT NULL,
                capture_duration_ms   REAL    NOT NULL,
                downscale_duration_ms REAL    NOT NULL,
                capture_width         INTEGER NOT NULL,
                capture_height        INTEGER NOT NULL,
                target_width          INTEGER NOT NULL,
                target_height         INTEGER NOT NULL,
                before_size_bytes     INTEGER NOT NULL,
                after_size_bytes      INTEGER NOT NULL
            )
        """)


def get_stats(db_path: Path = METRICS_DB_PATH) -> dict:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("""
            SELECT
                COUNT(*),
                AVG(capture_duration_ms),   MIN(capture_duration_ms),   MAX(capture_duration_ms),
                AVG(downscale_duration_ms), MIN(downscale_duration_ms), MAX(downscale_duration_ms),
                AVG(before_size_bytes),     MIN(before_size_bytes),     MAX(before_size_bytes),
                AVG(after_size_bytes),      MIN(after_size_bytes),      MAX(after_size_bytes),
                AVG(CAST(after_size_bytes AS REAL) / before_size_bytes),
                MIN(CAST(after_size_bytes AS REAL) / before_size_bytes),
                MAX(CAST(after_size_bytes AS REAL) / before_size_bytes)
            FROM capture_metrics
        """).fetchone()

    def r(v):
        return round(v, 2) if v is not None else None

    total = row[0]
    return {
        "total_captures": total,
        "capture_duration_ms":   {"avg": r(row[1]),  "min": r(row[2]),  "max": r(row[3])},
        "downscale_duration_ms": {"avg": r(row[4]),  "min": r(row[5]),  "max": r(row[6])},
        "before_size_bytes":     {"avg": r(row[7]),  "min": row[8],     "max": row[9]},
        "after_size_bytes":      {"avg": r(row[10]), "min": row[11],    "max": row[12]},
        "compression_ratio":     {"avg": r(row[13]), "min": r(row[14]), "max": r(row[15])},
    }


def record_capture(metrics: CaptureMetrics, db_path: Path = METRICS_DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO capture_metrics (
                captured_at, capture_duration_ms, downscale_duration_ms,
                capture_width, capture_height, target_width, target_height,
                before_size_bytes, after_size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metrics.captured_at,
                metrics.capture_duration_ms,
                metrics.downscale_duration_ms,
                metrics.capture_width,
                metrics.capture_height,
                metrics.target_width,
                metrics.target_height,
                metrics.before_size_bytes,
                metrics.after_size_bytes,
            ),
        )
