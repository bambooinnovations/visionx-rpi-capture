import os
import shutil
import threading
import time
from pathlib import Path

import structlog

logger = structlog.get_logger()

CAPTURE_TMP_DIR = Path(os.environ.get("CAPTURE_TMP_DIR", "/tmp/visionx_captures"))
CLEANUP_INTERVAL_SECONDS = 5 * 60  # run every 5 minutes
MAX_AGE_SECONDS = 5 * 60  # delete dirs older than 5 minutes


def _cleanup_stale_tmp_dirs() -> None:
    """Delete per-request subdirectories inside CAPTURE_TMP_DIR older than MAX_AGE_SECONDS.

    app.py creates one subdir per request via tempfile.mkdtemp(dir=CAPTURE_TMP_DIR).
    Normally cleaned up via after_this_request, but crashes can leave orphans.
    """
    if not CAPTURE_TMP_DIR.exists():
        return
    now = time.time()
    for entry in CAPTURE_TMP_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            age = now - entry.stat().st_mtime
            if age > MAX_AGE_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info(
                    "cleaned_stale_tmp_dir", path=str(entry), age_seconds=round(age)
                )
        except OSError:
            pass  # dir may have been removed concurrently


def _run_cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            _cleanup_stale_tmp_dirs()
        except Exception:
            logger.exception("tmp_cleanup_error")


def start_cleanup_task() -> None:
    """Start the background cleanup thread. Call once at app startup."""
    t = threading.Thread(target=_run_cleanup_loop, daemon=True, name="tmp-cleanup")
    t.start()
    logger.info(
        "cleanup_task_started",
        interval_seconds=CLEANUP_INTERVAL_SECONDS,
        max_age_seconds=MAX_AGE_SECONDS,
    )
