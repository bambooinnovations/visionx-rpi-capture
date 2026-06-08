import shutil
import threading
import time
from pathlib import Path

import structlog

import config
import runtime_config

logger = structlog.get_logger()

CAPTURE_TMP_DIR = config.CAPTURE_TMP_DIR

_evict_lock = threading.Lock()


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


def enforce_local_limits(save_dir: Path) -> None:
    """Delete the oldest files in save_dir until both count and size limits are met.

    Limits are read from runtime_config (live-updatable) with config.py as fallback,
    so a PATCH /rpi/config takes effect on the very next triggered capture.
    A limit of 0 means unlimited.
    """
    if not save_dir.exists():
        return

    with _evict_lock:
        max_files = runtime_config.get("hw_trigger.local_max_files", config.HW_TRIGGER_LOCAL_MAX_FILES)
        max_mb = runtime_config.get("hw_trigger.local_max_mb", config.HW_TRIGGER_LOCAL_MAX_MB)

        def _mtime(f: Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0

        # Sort oldest-first so we always remove the least recent captures.
        files = sorted(save_dir.glob("*.jpg"), key=_mtime)

        def _evict(f: Path) -> None:
            f.unlink(missing_ok=True)
            f.with_suffix(".jpg.json").unlink(missing_ok=True)

        if max_files > 0:
            while len(files) > max_files:
                removed = files.pop(0)
                _evict(removed)
                logger.info("hw_capture_evicted_count", path=str(removed))

        if max_mb > 0:
            max_bytes = max_mb * 1024 * 1024
            total = 0
            for f in files:
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
            while total > max_bytes and files:
                removed = files.pop(0)
                try:
                    total -= removed.stat().st_size
                    _evict(removed)
                    logger.info("hw_capture_evicted_size", path=str(removed))
                except OSError:
                    pass


def start_cleanup_task() -> None:
    """Start the background cleanup thread. Call once at app startup."""
    t = threading.Thread(target=_run_cleanup_loop, daemon=True, name="tmp-cleanup")
    t.start()
    logger.info(
        "cleanup_task_started",
        interval_seconds=config.CLEANUP_INTERVAL_SECONDS,
        max_age_seconds=config.MAX_AGE_SECONDS,
    )
