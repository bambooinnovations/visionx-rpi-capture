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


def _upload_image(jpeg_bytes: bytes, trigger_event: dict, url: str | None = None, filename: str = "capture.jpg", is_raw: bool = False) -> bool:
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
                    "trigger_source": "manual" if trigger_event.get("source") == "serial" else trigger_event.get("source", ""),
                },
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            _log = logger.debug if is_raw else logger.info
            _log(
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
    """Write jpeg_bytes to the local save dir. Returns the saved path.

    A sidecar <filename>.json is written alongside the image so that
    _disk_retry_loop can reconstruct the trigger metadata on retry.
    """
    import json

    save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        if filename is None:
            ts = int(time.time() * 1000)
            tnum = trigger_event.get("trigger", 0)
            filename = f"trigger_{tnum:06d}_{ts}.jpg"
        path = save_dir / filename
        path.write_bytes(jpeg_bytes)
        # Sidecar holds all trigger metadata needed for a successful retry upload.
        sidecar = path.with_suffix(".jpg.json")
        try:
            sidecar.write_text(json.dumps({k: v for k, v in trigger_event.items() if k != "_received_at"}))
        except Exception:
            pass  # missing sidecar degrades gracefully; retry will still attempt upload
        enforce_local_limits(save_dir)
        logger.info("hw_trigger_saved_local", path=str(path), trigger=trigger_event)
        return path
    except Exception as exc:
        logger.warning("hw_trigger_local_save_failed", error=str(exc))
        return None


class _TriggerCollector:
    """Gathers per-camera frames for each trigger and fires a callback when all arrive.

    Each camera worker calls add_frame() or failure() exactly once per trigger.
    When every camera has reported, on_complete(event, raw_captures, ts_ms) is called
    from whichever camera thread reported last.
    """

    _STALE_ENTRY_AGE_S = 30.0

    def __init__(self, camera_ids: set[int], on_complete) -> None:
        self._camera_ids = camera_ids
        self._on_complete = on_complete
        self._lock = threading.Lock()
        # trigger_num → {"event", "frames", "reported", "ts_ms"}
        self._pending: dict[int, dict] = {}

    def add_frame(self, event: dict, cam_id: int, jpeg_bytes: bytes, serial: str) -> None:
        self._report(event, cam_id, frame=(jpeg_bytes, serial))

    def failure(self, event: dict, cam_id: int) -> None:
        self._report(event, cam_id, frame=None)

    def _report(self, event: dict, cam_id: int, frame) -> None:
        trigger_num = event.get("trigger", id(event))
        callback = None
        with self._lock:
            if trigger_num not in self._pending:
                self._pending[trigger_num] = {
                    "event": event,
                    "frames": {},
                    "reported": set(),
                    "ts_ms": int(time.time() * 1000),
                }
            entry = self._pending[trigger_num]
            entry["reported"].add(cam_id)
            if frame is not None:
                entry["frames"][cam_id] = frame

            if entry["reported"] >= self._camera_ids:
                del self._pending[trigger_num]
                if entry["frames"]:
                    callback = (entry["event"], entry["frames"], entry["ts_ms"])

            self._evict_stale()

        if callback is not None:
            self._on_complete(*callback)

    def _evict_stale(self) -> None:
        """Remove entries that have been waiting too long (called under lock)."""
        cutoff = time.time() - self._STALE_ENTRY_AGE_S
        stale = [k for k, v in self._pending.items() if v["ts_ms"] / 1000 < cutoff]
        for k in stale:
            del self._pending[k]


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

        # One dedicated queue + thread per camera so each camera fires immediately
        # on trigger receipt with zero queueing delay regardless of upload backlog.
        self._camera_queues: dict[int, queue.Queue] = {
            cam_id: queue.Queue() for cam_id in cameras
        }
        self._camera_threads: list[threading.Thread] = []
        self._collector = _TriggerCollector(
            camera_ids=set(cameras.keys()),
            on_complete=self._on_all_captured,
        )

        # Two separate pools enforce stitch priority: stitch jobs never wait behind
        # raw-image uploads from previous triggers.
        self._stitch_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._raw_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._disk_retry_thread: threading.Thread | None = None

        self._pool_counter_lock = threading.Lock()
        self._stitch_pending = 0
        self._raw_pending = 0

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

        # Simulator state (independent of the serial listener).
        self._sim_thread: threading.Thread | None = None
        self._sim_stop_event = threading.Event()
        self._sim_speed_cms: float = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def simulator_running(self) -> bool:
        return self._sim_thread is not None and self._sim_thread.is_alive()

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

            # Stitch pool: compute stitch + upload stitch. Dedicated workers so this
            # critical path is never queued behind anything else.
            self._stitch_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="hw-stitch"
            )
            # Raw pool: single worker, intentionally serial and slow.
            # Raw image uploads are debug-only and must not compete with stitch
            # for network bandwidth. One worker ensures they drain quietly in the
            # background without ever affecting stitch upload latency.
            self._raw_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="hw-raw"
            )

            # Background thread: retry any disk-backed images (written on upload failure).
            self._disk_retry_thread = threading.Thread(
                target=self._disk_retry_loop, daemon=True, name="hw-disk-retry"
            )
            self._disk_retry_thread.start()

            # Start one dedicated capture thread per camera.
            self._camera_threads = []
            for cam_id, cam in self._cameras.items():
                t = threading.Thread(
                    target=self._camera_worker,
                    args=(cam_id, cam),
                    daemon=True,
                    name=f"hw-cam-{cam_id}",
                )
                t.start()
                self._camera_threads.append(t)

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

        # Unblock camera threads so they can see the stop event.
        for q in self._camera_queues.values():
            try:
                q.put_nowait(None)
            except Exception:
                pass
        for t in self._camera_threads:
            t.join(timeout=3)
        self._camera_threads = []

        with self._state_lock:
            self._arduino_state["serial_connected"] = False

        if self._stitch_pool is not None:
            self._stitch_pool.shutdown(wait=False, cancel_futures=True)
            self._stitch_pool = None
        if self._raw_pool is not None:
            self._raw_pool.shutdown(wait=False, cancel_futures=True)
            self._raw_pool = None

        if self._disk_retry_thread is not None:
            self._disk_retry_thread.join(timeout=5)
            self._disk_retry_thread = None

        logger.info("serial_trigger_listener_stopped")

    def start_simulator(self, speed_cms: float) -> None:
        """Start the simulator.

        Sends fire_trigger commands to the Arduino over serial at the interval
        derived from capture_interval_mm ÷ speed_cms. Requires the serial
        listener to already be running.
        """
        if self.simulator_running:
            raise RuntimeError("Simulator is already running")
        if not self.running:
            raise RuntimeError("Serial listener must be running before starting the simulator")
        self._sim_stop_event.clear()
        self._sim_speed_cms = speed_cms
        self._sim_thread = threading.Thread(
            target=self._run_simulator,
            args=(speed_cms,),
            daemon=True,
            name="hw-trigger-sim",
        )
        self._sim_thread.start()
        logger.info("decoder_simulator_started", speed_cms=speed_cms)

    def stop_simulator(self) -> None:
        """Stop the simulator timer."""
        self._sim_stop_event.set()
        if self._sim_thread is not None:
            self._sim_thread.join(timeout=5)
        self._sim_thread = None
        logger.info("decoder_simulator_stopped")

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
            "simulator_running": self.simulator_running,
            "simulator_speed_cms": self._sim_speed_cms if self.simulator_running else None,
            **stats_snapshot,
            **state,
        }

    def queue_depths(self) -> dict:
        """Return a snapshot of backlog depths for all pipeline queues."""
        camera_queues = {cam_id: q.qsize() for cam_id, q in self._camera_queues.items()}
        with self._pool_counter_lock:
            stitch_pending = self._stitch_pending
            raw_pending = self._raw_pending
        with self._collector._lock:
            collector_pending = len(self._collector._pending)
        save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
        disk_retry = sum(1 for _ in save_dir.glob("*.jpg")) if save_dir.exists() else 0
        return {
            "camera_queues": camera_queues,
            "collector_pending": collector_pending,
            "stitch_pending": stitch_pending,
            "raw_pending": raw_pending,
            "disk_retry": disk_retry,
        }

    # ── Private ───────────────────────────────────────────────────────────────

    def _stitch_submit(self, fn, *args) -> None:
        with self._pool_counter_lock:
            pool = self._stitch_pool
            if pool is None:
                return
            self._stitch_pending += 1
        def _run():
            try:
                fn(*args)
            finally:
                with self._pool_counter_lock:
                    self._stitch_pending = max(0, self._stitch_pending - 1)
        pool.submit(_run)

    def _raw_submit(self, fn, *args) -> None:
        with self._pool_counter_lock:
            pool = self._raw_pool
            if pool is None:
                return
            self._raw_pending += 1
        def _run():
            try:
                fn(*args)
            finally:
                with self._pool_counter_lock:
                    self._raw_pending = max(0, self._raw_pending - 1)
        pool.submit(_run)

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

    def _run_simulator(self, speed_cms: float) -> None:
        """Send fire_trigger to the Arduino at the interval matching speed_cms."""
        try:
            while not self._sim_stop_event.is_set():
                cfg = self._load_file_config()
                interval_mm = float(cfg.get("capture_interval_mm", PHYSICAL_DEFAULTS["capture_interval_mm"]))
                interval_s = (interval_mm / 10.0) / speed_cms

                self._sim_stop_event.wait(timeout=interval_s)
                if self._sim_stop_event.is_set():
                    break

                try:
                    self.send_command({"cmd": "fire_trigger"})
                except RuntimeError:
                    logger.warning("simulator_stopped_listener_not_running")
                    break
        except Exception:
            logger.exception("decoder_simulator_error")

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
            logger.debug(
                "serial_trigger_received",
                source=event.get("source"),
                count=event.get("count"),
                trigger=event.get("trigger"),
                speed_cms=event.get("speed_cms"),
            )

            event["_received_at"] = time.monotonic()
            for q in self._camera_queues.values():
                q.put(event)

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

    def _camera_worker(self, cam_id: int, cam: "MindVisionCamera") -> None:
        """Dedicated capture thread for one camera.

        Blocks on its queue and fires capture immediately when a trigger arrives.
        Never waits on uploads or other cameras — guaranteed zero queueing delay.
        """
        tmp_dir = config.CAPTURE_TMP_DIR / "hw_trigger"

        while not self._stop_event.is_set():
            try:
                event = self._camera_queues[cam_id].get(timeout=1.0)
            except queue.Empty:
                continue

            if event is None:  # sentinel from stop()
                break

            received_at = event.get("_received_at")
            if received_at is not None:
                queue_age = time.monotonic() - received_at
                max_age = float(runtime_config.get("hw_trigger.max_queue_age_s", 5.0))
                if queue_age > max_age:
                    logger.warning(
                        "hw_trigger_dropped_stale",
                        camera_id=cam_id,
                        queue_age_s=round(queue_age, 2),
                        trigger=event,
                    )
                    self._collector.failure(event, cam_id)
                    continue

            try:
                tmp_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=tmp_dir) as td:
                    image_path, _ = cam.capture_image(output_folder=Path(td))
                    jpeg_bytes = image_path.read_bytes()
                serial = cam.serial_number or str(cam_id)
                with self._stats_lock:
                    self._stats["captures_ok"] += 1
                logger.debug("hw_trigger_captured", camera_id=cam_id, trigger=event)
                self._collector.add_frame(event, cam_id, jpeg_bytes, serial)
            except Exception as exc:
                with self._stats_lock:
                    self._stats["captures_failed"] += 1
                logger.warning("hw_trigger_capture_failed", camera_id=cam_id, error=str(exc))
                self._collector.failure(event, cam_id)

    def _on_all_captured(
        self,
        event: dict,
        raw_captures: dict[int, tuple[bytes, str]],
        ts_ms: int,
    ) -> None:
        """Called by _TriggerCollector once every camera has reported for a trigger."""
        self._stitch_submit(self._stitch_and_upload, event, raw_captures, ts_ms)

    def _stitch_and_upload(
        self,
        event: dict,
        raw_captures: dict[int, tuple[bytes, str]],
        ts_ms: int,
    ) -> None:
        """Compute stitch and upload it immediately. Raw uploads go to the lower-priority pool.

        Runs in _stitch_pool (dedicated workers) so stitch uploads are never queued
        behind pending raw uploads from earlier triggers.
        Images stay in memory; disk is only written if the upload fails completely.
        """
        import cv2
        import numpy as np

        stitch_filename = f"{ts_ms}_stitch.jpg"
        stitch_bytes: bytes | None = None

        if len(raw_captures) >= 2 and self._load_calibration and self._stitch_frames:
            cal = self._load_calibration()
            if cal:
                import time as _time
                frames: dict[int, np.ndarray] = {}
                t_decode = 0.0
                for cam_id, (jpeg_bytes, _) in raw_captures.items():
                    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    _t0 = _time.perf_counter()
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    t_decode += _time.perf_counter() - _t0
                    if frame is not None:
                        frames[cam_id] = frame

                _t0 = _time.perf_counter()
                stitched = self._stitch_frames(frames, cal) if frames else None
                t_stitch = _time.perf_counter() - _t0

                if stitched is not None:
                    _t0 = _time.perf_counter()
                    ok_enc, buf = cv2.imencode(".jpg", stitched, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    t_encode = _time.perf_counter() - _t0
                    if ok_enc:
                        stitch_bytes = buf.tobytes()
                        logger.info("hw_trigger_stitched", trigger=event,
                                    t_stitch_ms=round(t_stitch * 1000),
                                    stitch_kb=round(len(stitch_bytes) / 1024))
                        logger.debug("hw_trigger_stitch_phases", trigger=event,
                                     t_decode_ms=round(t_decode * 1000),
                                     t_encode_ms=round(t_encode * 1000))
                    else:
                        logger.warning("hw_trigger_stitch_encode_failed", trigger=event)
                else:
                    logger.warning("hw_trigger_stitch_failed", trigger=event)
            else:
                logger.warning("hw_trigger_no_stitch_calibration", trigger=event)

        if stitch_bytes is None:
            first_cam = next(iter(raw_captures))
            stitch_bytes, _ = raw_captures[first_cam]

        # === STITCH UPLOAD — this is the entire purpose of this worker ===
        # Upload completes fully before this function returns. The raw pool is only
        # notified after this point, so raw uploads can never overlap with stitch uploads
        # from the same trigger and cannot consume bandwidth before stitch is done.
        stitch_ok = False
        url = _get_destination_url()
        if url:
            stitch_ok = _upload_image(stitch_bytes, event, url=url, filename=stitch_filename)
            with self._stats_lock:
                if stitch_ok:
                    self._stats["uploads_ok"] += 1
                else:
                    self._stats["uploads_failed"] += 1

        # Save locally if explicitly requested OR as fallback when upload failed.
        if not stitch_ok and _get_save_local():
            _save_image_locally(stitch_bytes, event, filename=stitch_filename)
            if url:
                logger.warning(
                    "hw_trigger_stitch_fallback_local",
                    filename=stitch_filename,
                    trigger=event,
                )

        # === RAW UPLOADS — debug only, enqueued after stitch is done ===
        # Submitted to the single-worker raw pool so they drain slowly in the
        # background and never contend with stitch uploads for network bandwidth.
        raw_url = _get_raw_destination_url() if _get_send_raw_images() else None
        if raw_url:
            self._raw_submit(self._upload_raw_captures, raw_captures, event, ts_ms, raw_url)

    def _wait_for_stitch_drain(self, poll_interval: float = 0.5) -> None:
        """Block until the stitch pool has no pending work, then return.

        Called by the raw upload worker before each upload so raw traffic
        never competes with stitch uploads for bandwidth.
        """
        while True:
            with self._pool_counter_lock:
                pending = self._stitch_pending
            if pending == 0:
                return
            if self._stop_event.wait(timeout=poll_interval):
                return

    def _upload_raw_captures(
        self,
        raw_captures: dict[int, tuple[bytes, str]],
        event: dict,
        ts_ms: int,
        raw_url: str,
    ) -> None:
        """Upload individual camera images. Debug only — runs in the single-worker raw pool.

        Yields to stitch uploads before starting: if the stitch pool has any
        pending work this method blocks until it drains, so raw traffic never
        competes with stitch for bandwidth.
        """
        self._wait_for_stitch_drain()
        for cam_id, (jpeg_bytes, serial) in raw_captures.items():
            filename = f"{ts_ms}_{serial}.jpg"
            ok = _upload_image(jpeg_bytes, event, url=raw_url, filename=filename, is_raw=True)
            with self._stats_lock:
                if ok:
                    self._stats["uploads_ok"] += 1
                else:
                    self._stats["uploads_failed"] += 1
                    _save_image_locally(jpeg_bytes, event, filename=filename)
                    logger.warning(
                        "hw_trigger_raw_fallback_local",
                        filename=filename,
                        trigger=event,
                    )

    def _disk_retry_loop(self) -> None:
        """Background thread: retry uploading any disk-backed images when the network recovers.

        Disk files only exist because a previous upload failed completely. Stitch files
        are retried before raw files to preserve priority.
        """
        while not self._stop_event.wait(timeout=60):
            save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
            if not save_dir.exists():
                continue

            stitch_files = sorted(save_dir.glob("*_stitch.jpg"))
            raw_files = [f for f in sorted(save_dir.glob("*.jpg")) if not f.name.endswith("_stitch.jpg")]

            for f in stitch_files + raw_files:
                if self._stop_event.is_set():
                    return
                is_stitch = f.name.endswith("_stitch.jpg")
                url = _get_destination_url() if is_stitch else _get_raw_destination_url()
                if not url:
                    continue
                try:
                    import json
                    sidecar = f.with_suffix(".jpg.json")
                    if sidecar.exists():
                        try:
                            trigger_event = json.loads(sidecar.read_text())
                        except Exception:
                            trigger_event = {}
                    else:
                        # No sidecar means this file predates the metadata-tracking fix.
                        # The stitch endpoint requires trigger fields; without them the server
                        # returns 422 every time. Delete the unrecoverable file rather than
                        # burning retries indefinitely.
                        logger.warning("hw_trigger_disk_retry_no_sidecar", filename=f.name)
                        f.unlink(missing_ok=True)
                        continue
                    image_bytes = f.read_bytes()
                    ok = _upload_image(image_bytes, trigger_event, url=url, filename=f.name)
                    if ok:
                        f.unlink(missing_ok=True)
                        sidecar.unlink(missing_ok=True)
                        logger.info("hw_trigger_disk_retry_ok", filename=f.name, url=url)
                except Exception as exc:
                    logger.warning("hw_trigger_disk_retry_error", filename=f.name, error=str(exc))
