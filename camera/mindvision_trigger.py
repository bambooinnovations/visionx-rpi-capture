"""Serial-based hardware trigger listener.

Reads JSON lines from arduino/decoder_trigger.ino over a serial port.
Each trigger event captures an image from the MindVision camera(s) and:
  - POSTs it to the configured destination URL (if set)
  - Saves a local copy (if hw_trigger.save_local is true)

Expected Arduino JSON format:
  {"type":"trigger","source":"encoder","count":118,"trigger":1}
  {"type":"trigger","source":"manual","count":0,"trigger":1}
"""
from __future__ import annotations

import io
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

import config
import runtime_config
from tasks import enforce_local_limits

if TYPE_CHECKING:
    from camera.mindvision import MindVisionCamera

logger = structlog.get_logger()


def _get_destination_url() -> str:
    return runtime_config.get("hw_trigger.destination_url", config.HW_TRIGGER_DESTINATION_URL)


def _get_api_key() -> str:
    return runtime_config.get("hw_trigger.destination_api_key", config.HW_TRIGGER_DESTINATION_API_KEY)


def _get_retry_attempts() -> int:
    return int(runtime_config.get("hw_trigger.retry_attempts", config.HW_TRIGGER_RETRY_ATTEMPTS))


def _get_timeout() -> int:
    return int(runtime_config.get("hw_trigger.timeout_seconds", config.HW_TRIGGER_TIMEOUT_SECONDS))


def _get_save_local() -> bool:
    return bool(runtime_config.get("hw_trigger.save_local", config.HW_TRIGGER_SAVE_LOCAL))


def _get_health_check_url() -> str:
    return config.HW_TRIGGER_HEALTH_CHECK_URL


def check_server_health() -> dict:
    """GET the configured health_check_url and return reachability result.

    Returns {"reachable": True} on any HTTP response (even 4xx — the server
    answered), or {"reachable": False, "error": "<message>"} on network/timeout
    failure. Returns None if no health_check_url is configured.
    """
    url = _get_health_check_url()
    if not url:
        return None

    import requests

    try:
        resp = requests.get(url, timeout=5)
        logger.info("hw_trigger_health_check_ok", url=url, status=resp.status_code)
        return {"reachable": True, "status_code": resp.status_code}
    except Exception as exc:
        logger.warning("hw_trigger_health_check_failed", url=url, error=str(exc))
        return {"reachable": False, "error": str(exc)}


def _upload_image(jpeg_bytes: bytes, trigger_event: dict) -> bool:
    """POST jpeg_bytes to the destination URL. Returns True on success."""
    import requests

    url = _get_destination_url()
    if not url:
        return False

    api_key = _get_api_key()
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    attempts = _get_retry_attempts()
    timeout = _get_timeout()

    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(
                url,
                files={"image": ("capture.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
                data={
                    "trigger_count": str(trigger_event.get("count", "")),
                    "trigger_number": str(trigger_event.get("trigger", "")),
                    "trigger_source": trigger_event.get("source", ""),
                },
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            logger.info(
                "hw_trigger_upload_ok",
                url=url,
                status=resp.status_code,
                trigger=trigger_event,
            )
            return True
        except Exception as exc:
            logger.warning(
                "hw_trigger_upload_failed",
                attempt=attempt,
                attempts=attempts,
                url=url,
                error=str(exc),
                trigger=trigger_event,
            )
            if attempt < attempts:
                time.sleep(1.0 * attempt)

    return False


def _save_image_locally(jpeg_bytes: bytes, trigger_event: dict) -> Path | None:
    """Write jpeg_bytes to the local save dir. Returns the saved path."""
    save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        tnum = trigger_event.get("trigger", 0)
        path = save_dir / f"trigger_{tnum:06d}_{ts}.jpg"
        path.write_bytes(jpeg_bytes)
        enforce_local_limits(save_dir)
        logger.info("hw_trigger_saved_local", path=str(path), trigger=trigger_event)
        return path
    except Exception as exc:
        logger.warning("hw_trigger_local_save_failed", error=str(exc))
        return None


class SerialTriggerListener:
    """Background thread that reads Arduino trigger events from a serial port."""

    def __init__(self, cameras: dict[int, "MindVisionCamera"]) -> None:
        self._cameras = cameras
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._stats = {
            "triggers_received": 0,
            "captures_ok": 0,
            "captures_failed": 0,
            "uploads_ok": 0,
            "uploads_failed": 0,
            "started_at": None,
        }

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, port: str, baud: int = 115200) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("Serial trigger listener is already running")
            self._stop_event.clear()
            self._stats["started_at"] = time.time()
            self._thread = threading.Thread(
                target=self._run,
                args=(port, baud),
                daemon=True,
                name="serial-trigger",
            )
            self._thread.start()
            logger.info("serial_trigger_listener_started", port=port, baud=baud)

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("serial_trigger_listener_stopped")

    def status(self) -> dict:
        uptime = None
        if self._stats["started_at"] is not None and self.running:
            uptime = round(time.time() - self._stats["started_at"], 1)
        return {
            "running": self.running,
            "uptime_seconds": uptime,
            **{k: v for k, v in self._stats.items() if k != "started_at"},
        }

    def _run(self, port: str, baud: int) -> None:
        import serial
        import serial.serialutil

        ser = None
        while not self._stop_event.is_set():
            try:
                if ser is None:
                    ser = serial.Serial(port, baud, timeout=1.0)
                    logger.info("serial_port_opened", port=port, baud=baud)

                line = ser.readline()
                if not line:
                    continue

                text = line.decode("ascii", errors="ignore").strip()
                if not text:
                    continue

                self._handle_line(text)

            except serial.serialutil.SerialException as exc:
                logger.warning("serial_port_error", port=port, error=str(exc))
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                if not self._stop_event.is_set():
                    time.sleep(2.0)

            except Exception:
                logger.exception("serial_trigger_unexpected_error")
                if not self._stop_event.is_set():
                    time.sleep(1.0)

        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _handle_line(self, text: str) -> None:
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return

        if event.get("type") != "trigger":
            return

        self._stats["triggers_received"] += 1
        logger.info(
            "serial_trigger_received",
            source=event.get("source"),
            count=event.get("count"),
            trigger=event.get("trigger"),
        )

        # Capture from all cameras concurrently.
        capture_threads = []
        for cam_id, cam in self._cameras.items():
            t = threading.Thread(
                target=self._capture_one,
                args=(cam_id, cam, event),
                daemon=True,
            )
            t.start()
            capture_threads.append(t)
        for t in capture_threads:
            t.join(timeout=30)

    def _capture_one(self, cam_id: int, cam: "MindVisionCamera", event: dict) -> None:
        try:
            tmp_dir = config.CAPTURE_TMP_DIR / "hw_trigger"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=tmp_dir) as td:
                image_path, _ = cam.capture_image(output_folder=Path(td))
                jpeg_bytes = image_path.read_bytes()

            self._stats["captures_ok"] += 1
            logger.info("hw_trigger_captured", camera_id=cam_id, trigger=event)
        except Exception as exc:
            self._stats["captures_failed"] += 1
            logger.warning("hw_trigger_capture_failed", camera_id=cam_id, error=str(exc))
            return

        if _get_save_local():
            _save_image_locally(jpeg_bytes, event)

        url = _get_destination_url()
        if url:
            ok = _upload_image(jpeg_bytes, event)
            if ok:
                self._stats["uploads_ok"] += 1
            else:
                self._stats["uploads_failed"] += 1
