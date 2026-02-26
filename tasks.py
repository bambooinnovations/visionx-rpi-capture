import shutil
import threading
import time

import structlog

import config

logger = structlog.get_logger()

CAPTURE_TMP_DIR = config.CAPTURE_TMP_DIR


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
            if age > config.MAX_AGE_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info(
                    "cleaned_stale_tmp_dir", path=str(entry), age_seconds=round(age)
                )
        except OSError:
            pass  # dir may have been removed concurrently


def _run_cleanup_loop() -> None:
    while True:
        time.sleep(config.CLEANUP_INTERVAL_SECONDS)
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
        interval_seconds=config.CLEANUP_INTERVAL_SECONDS,
        max_age_seconds=config.MAX_AGE_SECONDS,
    )
