"""Serial-based hardware trigger listener with bidirectional Arduino control.

Reads JSON lines from arduino/decoder_trigger.ino over a serial port.
Each trigger event captures an image from the MindVision camera(s) and:
  - POSTs it to the configured destination URL (if set)
  - Saves a local copy (if hw_trigger.save_local is true)

Arduino → RPi messages:
  {"type":"trigger","source":"encoder","count":118,"trigger":1,"speed_cms":5.20}
  {"type":"speed","speed_cms":5.20,"count":118,"trigger_enabled":true}
  {"type":"config","trigger_enabled":true,"trigger_interval":118,...}
  {"type":"ack","cmd":"set_trigger_interval","ok":true}
  {"type":"startup","msg":"Trigger controller started"}

RPi → Arduino commands (queued via send_command()):
  {"cmd":"get_config"}
  {"cmd":"reset_count"}
  {"cmd":"set_trigger_enabled","value":true}
  {"cmd":"set_trigger_interval","value":118}
  {"cmd":"set_counts_per_cm","value":118.0}
  {"cmd":"set_pulse_width_ms","value":20}
  {"cmd":"set_speed_report_interval_ms","value":500}

On connect, the listener pushes values from data/arduino_config.json to the
Arduino so parameters survive RPi restarts without reflashing the sketch.
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import queue
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

# Persisted Arduino parameter overrides (survives RPi restart, no reflash needed).
ARDUINO_CONFIG_PATH = Path("data/arduino_config.json")

# Arduino compile-time defaults — used when resetting to factory state.
ARDUINO_DEFAULTS: dict = {
    "trigger_interval": 118,
    "counts_per_cm": 118.0,
    "pulse_width_ms": 20,
    "speed_report_interval_ms": 500,
}

# Keys the RPi is allowed to set on the Arduino (raw counts-based params).
ARDUINO_SETTABLE_KEYS: dict[str, type] = {
    "trigger_interval": int,
    "counts_per_cm": float,
    "pulse_width_ms": int,
    "speed_report_interval_ms": int,
}

# Human-readable physical parameters — stored in config file, never sent to Arduino.
# The system derives trigger_interval and counts_per_cm from these.
PHYSICAL_DEFAULTS: dict = {
    "wheel_diameter_mm": 64.7,    # encoder wheel diameter in mm (as given by manufacturer)
    "encoder_ppr": 600,           # encoder pulses per revolution
    "capture_interval_mm": 10.0,  # desired distance between captures in mm
}

PHYSICAL_SETTABLE_KEYS: dict[str, type] = {
    "wheel_diameter_mm": float,
    "encoder_ppr": int,
    "capture_interval_mm": float,
}


def compute_arduino_params(diameter_mm: float, ppr: int, interval_mm: float) -> dict:
    """Derive counts_per_cm and trigger_interval from physical wheel/encoder values.

    circumference = π × diameter
    Quadrature decoding gives 4× the encoder PPR as counts per revolution.
    """
    import math
    circumference_mm = math.pi * diameter_mm
    counts_per_cm = (ppr * 4) / (circumference_mm / 10.0)
    trigger_interval = max(1, round(counts_per_cm * interval_mm / 10.0))
    return {
        "counts_per_cm": round(counts_per_cm, 4),
        "trigger_interval": trigger_interval,
    }


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


def _get_send_raw_images() -> bool:
    return bool(runtime_config.get("hw_trigger.send_raw_images", config.HW_TRIGGER_SEND_RAW_IMAGES))


def _get_raw_destination_url() -> str:
    return runtime_config.get("hw_trigger.raw_destination_url", config.HW_TRIGGER_RAW_DESTINATION_URL)


def _get_health_check_url() -> str:
    return config.HW_TRIGGER_HEALTH_CHECK_URL


def check_server_health() -> dict:
    """GET the configured health_check_url and return reachability result."""
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


def _upload_image(jpeg_bytes: bytes, trigger_event: dict, url: str | None = None, filename: str = "capture.jpg") -> bool:
    """POST jpeg_bytes to url (defaults to destination_url). Returns True on success."""
    import requests

    if url is None:
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
                files={"image": (filename, io.BytesIO(jpeg_bytes), "image/jpeg")},
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
                filename=filename,
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
                filename=filename,
                error=str(exc),
                trigger=trigger_event,
            )
            if attempt < attempts:
                time.sleep(1.0 * attempt)

    return False


def _save_image_locally(jpeg_bytes: bytes, trigger_event: dict, filename: str | None = None) -> Path | None:
    """Write jpeg_bytes to the local save dir. Returns the saved path."""
    save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        if filename is None:
            ts = int(time.time() * 1000)
            tnum = trigger_event.get("trigger", 0)
            filename = f"trigger_{tnum:06d}_{ts}.jpg"
        path = save_dir / filename
        path.write_bytes(jpeg_bytes)
        enforce_local_limits(save_dir)
        logger.info("hw_trigger_saved_local", path=str(path), trigger=trigger_event)
        return path
    except Exception as exc:
        logger.warning("hw_trigger_local_save_failed", error=str(exc))
        return None


class SerialTriggerListener:
    """Background thread that reads Arduino trigger events and sends commands."""

    def __init__(
        self,
        cameras: dict[int, "MindVisionCamera"],
        load_calibration=None,
        stitch_frames=None,
    ) -> None:
        self._cameras = cameras
        # Optional stitch helpers injected by the caller so this module does not
        # import from the blueprints layer (avoids a circular dependency).
        self._load_calibration = load_calibration
        self._stitch_frames = stitch_frames
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._send_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._state_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        # Captures and uploads run in a thread pool so the serial I/O loop is
        # never blocked waiting for image grabs or network uploads.
        self._capture_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="hw-capture"
        )

        self._stats = {
            "triggers_received": 0,
            "captures_ok": 0,
            "captures_failed": 0,
            "uploads_ok": 0,
            "uploads_failed": 0,
            "started_at": None,
        }

        # Live state mirrored from Arduino messages.
        self._arduino_state: dict = {
            "serial_connected": False,
            "speed_cms": 0.0,
            "last_message_at": None,
            "trigger_enabled": True,
            "encoder_count": 0,
            "arduino_config": {},
        }

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def serial_connected(self) -> bool:
        with self._state_lock:
            return bool(self._arduino_state.get("serial_connected", False))

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
        with self._state_lock:
            self._arduino_state["serial_connected"] = False
        logger.info("serial_trigger_listener_stopped")

    def send_command(self, cmd: dict) -> None:
        """Queue a JSON command to be sent to the Arduino on the next loop tick."""
        if not self.running:
            raise RuntimeError("Serial trigger listener is not running")
        self._send_queue.put(json.dumps(cmd))

    def save_config(self, updates: dict) -> None:
        """Persist parameter overrides to arduino_config.json."""
        cfg = self._load_file_config()
        cfg.update(updates)
        try:
            ARDUINO_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            ARDUINO_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        except Exception as exc:
            logger.warning("arduino_config_save_failed", error=str(exc))

    def reset_config(self) -> None:
        """Delete arduino_config.json so Arduino defaults take effect on next connect."""
        try:
            ARDUINO_CONFIG_PATH.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("arduino_config_reset_failed", error=str(exc))

    def set_trigger_state(self, enabled: bool) -> None:
        """Optimistically update trigger_enabled in the mirrored Arduino state."""
        with self._state_lock:
            self._arduino_state["trigger_enabled"] = enabled

    def file_config(self) -> dict:
        """Return the full saved config (physical + raw params) from disk."""
        return self._load_file_config()

    def status(self) -> dict:
        with self._stats_lock:
            uptime = None
            if self._stats["started_at"] is not None and self.running:
                uptime = round(time.time() - self._stats["started_at"], 1)
            stats_snapshot = {k: v for k, v in self._stats.items() if k != "started_at"}
        with self._state_lock:
            state = dict(self._arduino_state)
        return {
            "running": self.running,
            "uptime_seconds": uptime,
            **stats_snapshot,
            **state,
        }

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_file_config(self) -> dict:
        try:
            if ARDUINO_CONFIG_PATH.exists():
                return json.loads(ARDUINO_CONFIG_PATH.read_text())
        except Exception as exc:
            logger.warning("arduino_config_load_failed", error=str(exc))
        return {}

    def _push_startup_config(self, ser) -> None:
        """Send get_config then push any persisted overrides from arduino_config.json."""
        try:
            ser.write((json.dumps({"cmd": "get_config"}) + "\n").encode("ascii"))
            ser.flush()
        except Exception as exc:
            logger.warning("arduino_get_config_failed", error=str(exc))
            return

        cfg = self._load_file_config()
        for key, value in cfg.items():
            if key not in ARDUINO_SETTABLE_KEYS:
                continue
            try:
                msg = json.dumps({"cmd": f"set_{key}", "value": value}) + "\n"
                ser.write(msg.encode("ascii"))
                ser.flush()
                time.sleep(0.05)
            except Exception as exc:
                logger.warning("arduino_config_push_failed", key=key, error=str(exc))

        if cfg:
            logger.info("arduino_config_pushed", keys=list(cfg.keys()))

    def _run(self, port: str, baud: int) -> None:
        import serial
        import serial.serialutil

        ser = None
        while not self._stop_event.is_set():
            try:
                if ser is None:
                    ser = serial.Serial(port, baud, timeout=0.1)
                    with self._state_lock:
                        self._arduino_state["serial_connected"] = True
                    logger.info("serial_port_opened", port=port, baud=baud)
                    # Arduino resets when DTR is asserted on open; wait for boot.
                    time.sleep(2.0)
                    self._push_startup_config(ser)

                # Drain any queued outgoing commands before blocking on read.
                while not self._send_queue.empty():
                    try:
                        msg = self._send_queue.get_nowait()
                        ser.write((msg + "\n").encode("ascii"))
                        ser.flush()
                    except Exception as exc:
                        logger.warning("serial_write_failed", error=str(exc))

                line = ser.readline()
                if not line:
                    continue

                text = line.decode("ascii", errors="ignore").strip()
                if text:
                    self._handle_line(text)

            except serial.serialutil.SerialException as exc:
                logger.warning("serial_port_error", port=port, error=str(exc))
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                with self._state_lock:
                    self._arduino_state["serial_connected"] = False
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

        msg_type = event.get("type")

        with self._state_lock:
            self._arduino_state["last_message_at"] = time.time()

        if msg_type == "trigger":
            with self._state_lock:
                self._arduino_state["speed_cms"] = event.get("speed_cms", 0.0)
                self._arduino_state["encoder_count"] = event.get("count", 0)

            with self._stats_lock:
                self._stats["triggers_received"] += 1
            logger.info(
                "serial_trigger_received",
                source=event.get("source"),
                count=event.get("count"),
                trigger=event.get("trigger"),
                speed_cms=event.get("speed_cms"),
            )

            self._capture_pool.submit(self._capture_and_stitch, event)

        elif msg_type == "speed":
            with self._state_lock:
                self._arduino_state["speed_cms"] = event.get("speed_cms", 0.0)
                self._arduino_state["encoder_count"] = event.get("count", 0)
                self._arduino_state["trigger_enabled"] = event.get("trigger_enabled", True)

        elif msg_type == "config":
            cfg = {k: v for k, v in event.items() if k != "type"}
            with self._state_lock:
                self._arduino_state["trigger_enabled"] = event.get("trigger_enabled", True)
                self._arduino_state["arduino_config"] = cfg
            logger.info("arduino_config_received", config=cfg)

        elif msg_type == "ack":
            logger.debug("arduino_ack", cmd=event.get("cmd"), ok=event.get("ok"))

        elif msg_type == "startup":
            logger.info("arduino_startup", msg=event.get("msg"))

    def _capture_and_stitch(self, event: dict) -> None:
        """Capture from all cameras, stitch, and upload. Called once per trigger event."""
        import cv2
        import numpy as np

        ts_ms = int(time.time() * 1000)
        tmp_dir = config.CAPTURE_TMP_DIR / "hw_trigger"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # --- 1. Capture raw frames from every camera ---
        raw_captures: dict[int, tuple[bytes, str]] = {}  # cam_id → (jpeg_bytes, serial)
        for cam_id, cam in self._cameras.items():
            try:
                with tempfile.TemporaryDirectory(dir=tmp_dir) as td:
                    image_path, _ = cam.capture_image(output_folder=Path(td))
                    jpeg_bytes = image_path.read_bytes()
                serial = cam.serial_number or str(cam_id)
                raw_captures[cam_id] = (jpeg_bytes, serial)
                with self._stats_lock:
                    self._stats["captures_ok"] += 1
                logger.info("hw_trigger_captured", camera_id=cam_id, trigger=event)
            except Exception as exc:
                with self._stats_lock:
                    self._stats["captures_failed"] += 1
                logger.warning("hw_trigger_capture_failed", camera_id=cam_id, error=str(exc))

        if not raw_captures:
            return

        # --- 2. Upload raw images if enabled ---
        if _get_send_raw_images():
            raw_url = _get_raw_destination_url()
            for cam_id, (jpeg_bytes, serial) in raw_captures.items():
                filename = f"{ts_ms}_{serial}.jpg"
                if _get_save_local():
                    _save_image_locally(jpeg_bytes, event, filename=filename)
                if raw_url:
                    ok = _upload_image(jpeg_bytes, event, url=raw_url, filename=filename)
                    with self._stats_lock:
                        if ok:
                            self._stats["uploads_ok"] += 1
                        else:
                            self._stats["uploads_failed"] += 1

        # --- 3. Stitch and upload ---
        stitch_filename = f"{ts_ms}_stitch.jpg"
        stitch_bytes: bytes | None = None

        if len(raw_captures) >= 2 and self._load_calibration and self._stitch_frames:
            cal = self._load_calibration()
            if cal:
                frames: dict[int, np.ndarray] = {}
                for cam_id, (jpeg_bytes, _) in raw_captures.items():
                    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        frames[cam_id] = frame

                stitched = self._stitch_frames(frames, cal) if frames else None
                if stitched is not None:
                    ok_enc, buf = cv2.imencode(".jpg", stitched, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if ok_enc:
                        stitch_bytes = buf.tobytes()
                        logger.info("hw_trigger_stitched", trigger=event)
                    else:
                        logger.warning("hw_trigger_stitch_encode_failed", trigger=event)
                else:
                    logger.warning("hw_trigger_stitch_failed", trigger=event)
            else:
                logger.warning("hw_trigger_no_stitch_calibration", trigger=event)

        if stitch_bytes is None:
            # Fallback: use the first available raw image as the "stitched" upload.
            first_cam = next(iter(raw_captures))
            stitch_bytes, _ = raw_captures[first_cam]

        if _get_save_local():
            _save_image_locally(stitch_bytes, event, filename=stitch_filename)

        url = _get_destination_url()
        if url:
            ok = _upload_image(stitch_bytes, event, url=url, filename=stitch_filename)
            with self._stats_lock:
                if ok:
                    self._stats["uploads_ok"] += 1
                else:
                    self._stats["uploads_failed"] += 1
