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
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
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

# Named speed presets ("styles") — saved physical params a caller can activate by name
# instead of resending the full wheel/encoder/interval payload every time.
SPEED_PRESETS_PATH = Path("data/speed_presets.json")

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
    "capture_interval_mm": 400.0,  # desired distance between captures in mm
}

PHYSICAL_SETTABLE_KEYS: dict[str, type] = {
    "wheel_diameter_mm": float,
    "encoder_ppr": int,
    "capture_interval_mm": float,
}

# Built-in speed preset seeded into every fresh deployment (see
# seed_default_speed_presets() below). wheel_diameter_mm reflects the physical
# 300mm inspection-machine roller (CKDL "Fabric Inspection Machine Speed"
# reference sheet); belt run speed (yds/min) is a separate physical dial on
# the machine, not something this preset controls, so it isn't captured here.
DEFAULT_SPEED_PRESETS: dict = {
    "default": {"wheel_diameter_mm": 300.0, "encoder_ppr": 600, "capture_interval_mm": 400.0},
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


def _load_speed_presets() -> dict:
    try:
        if SPEED_PRESETS_PATH.exists():
            return json.loads(SPEED_PRESETS_PATH.read_text())
    except Exception as exc:
        logger.warning("speed_presets_load_failed", error=str(exc))
    return {}


def _save_speed_presets(presets: dict) -> None:
    try:
        SPEED_PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SPEED_PRESETS_PATH.write_text(json.dumps(presets, indent=2))
    except Exception as exc:
        logger.warning("speed_presets_save_failed", error=str(exc))


def list_speed_presets() -> dict:
    """Return all saved {style: physical_params} presets."""
    return _load_speed_presets()


def get_speed_preset(style: str) -> dict | None:
    return _load_speed_presets().get(style)


def save_speed_preset(style: str, updates: dict) -> dict:
    """Merge updates into the named preset (creating it if new) and persist. Does not touch the Arduino."""
    presets = _load_speed_presets()
    merged = {**presets.get(style, {}), **updates}
    presets[style] = merged
    _save_speed_presets(presets)
    return merged


def delete_speed_preset(style: str) -> bool:
    presets = _load_speed_presets()
    if style not in presets:
        return False
    del presets[style]
    _save_speed_presets(presets)
    return True


def seed_default_speed_presets() -> None:
    """Write DEFAULT_SPEED_PRESETS to disk if no presets file exists yet.

    Runs once per deployment: after the first write, data/speed_presets.json
    exists on disk even if every preset in it is later deleted, so this never
    re-seeds over an operator's intentional changes.
    """
    if SPEED_PRESETS_PATH.exists():
        return
    _save_speed_presets(dict(DEFAULT_SPEED_PRESETS))
    logger.info("speed_presets_seeded", styles=list(DEFAULT_SPEED_PRESETS.keys()))


_CAM_FILE_RE = re.compile(r"_cam(\d+)\.jpg$")  # a per-camera upload that failed and was saved for retry


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


def _get_use_stitch() -> bool:
    # DEPRECATED: stitching is archived (see archive/stitch/) and no longer wired up.
    # The stitched-upload branch below is kept for reference only and never runs.
    return False


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


def _iso_from_ts_ms(ts_ms: int) -> str:
    """Convert a Pi-clock epoch-ms timestamp to an ISO-8601 UTC string."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _upload_image(jpeg_bytes: bytes, trigger_event: dict, url: str | None = None, filename: str = "capture.jpg", is_raw: bool = False, camera_id: int | None = None, ts_ms: int | None = None, timing: dict | None = None, abort=None) -> bool:
    """POST jpeg_bytes to url (defaults to destination_url). Returns True on success.

    `abort`, if given, is polled before every attempt; when it returns True the upload is given up (returns False)
    — how a force-stopped task stops retrying. A request already on the wire is not interrupted.

    `timing` (queue/capture/decode/stitch/encode durations gathered upstream) is sent
    as a JSON form field so the server can persist it for pipeline-performance analysis.
    Upload duration itself can't be included here (it isn't known until after the
    request completes) — it's only logged locally, and the server should derive
    network/handling time from its own receive timestamp vs. `captured_at`.
    """
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

    data = {
        "trigger_count": str(trigger_event.get("count", "")),
        "trigger_number": str(trigger_event.get("trigger", "")),
        "trigger_source": "manual" if trigger_event.get("source") == "serial" else trigger_event.get("source", ""),
    }
    if trigger_event.get("task_id"):
        data["task_id"] = str(trigger_event["task_id"])
    if camera_id is not None:
        data["camera_id"] = str(camera_id)
    if ts_ms is not None:
        data["captured_at"] = _iso_from_ts_ms(ts_ms)
    if timing is not None:
        data["timing"] = json.dumps(timing)

    for attempt in range(1, attempts + 1):
        if abort is not None and abort():
            return False
        try:
            _t0 = time.perf_counter()
            resp = requests.post(
                url,
                files={"image": (filename, io.BytesIO(jpeg_bytes), "image/jpeg")},
                data=data,
                headers=headers,
                timeout=timeout,
            )
            upload_ms = round((time.perf_counter() - _t0) * 1000, 2)
            resp.raise_for_status()
            _log = logger.debug if is_raw else logger.info
            _log(
                "hw_trigger_upload_ok",
                url=url,
                filename=filename,
                status=resp.status_code,
                upload_ms=upload_ms,
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


def _save_image_locally(jpeg_bytes: bytes, trigger_event: dict, filename: str | None = None, ts_ms: int | None = None, timing: dict | None = None) -> Path | None:
    """Write jpeg_bytes to the local save dir. Returns the saved path.

    A sidecar <filename>.json is written alongside the image so that
    _disk_retry_loop can reconstruct the trigger metadata (including ts_ms and the
    original pipeline timing, so a retried upload still carries the original
    captured_at and timing instead of losing them) on retry.
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
            sidecar_data = {k: v for k, v in trigger_event.items() if k not in ("_received_at", "_gen")}
            if ts_ms is not None:
                sidecar_data["ts_ms"] = ts_ms
            if timing is not None:
                sidecar_data["timing"] = timing
            sidecar.write_text(json.dumps(sidecar_data))
        except Exception:
            pass  # missing sidecar degrades gracefully; retry will still attempt upload
        enforce_local_limits(save_dir)
        logger.info("hw_trigger_saved_local", path=str(path), trigger=trigger_event)
        return path
    except Exception as exc:
        logger.warning("hw_trigger_local_save_failed", error=str(exc))
        return None


def is_serial_url(port: str) -> bool:
    """True for a pyserial URL (socket://, rfc2217://, ...) rather than a device path."""
    return "://" in str(port)


def url_ports_allowed() -> bool:
    """Serial URLs may only be used with the mock hardware (camera type mock_mindvision)."""
    return config.CAMERA_TYPE == "mock_mindvision"


def distance_mm_from_count(count: int | float, counts_per_cm: float) -> float | None:
    """Travelled distance in mm for an Arduino running encoder count; None if the scale is unusable."""
    if not counts_per_cm or counts_per_cm <= 0:
        return None
    return round(count / counts_per_cm * 10.0, 1)


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

    def add_frame(self, event: dict, cam_id: int, jpeg_bytes: bytes, serial: str, timing: dict) -> None:
        self._report(event, cam_id, frame=(jpeg_bytes, serial, timing))

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
        # Bounded: triggers are dropped (with a warning) when the queue is full so a
        # burst of Arduino pulses cannot grow the queue without bound.
        _trigger_maxsize = int(runtime_config.get("hw_trigger.trigger_queue_maxsize", 30))
        self._camera_queues: dict[int, queue.Queue] = {
            cam_id: queue.Queue(maxsize=_trigger_maxsize) for cam_id in cameras
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
        self._stitch_pending_bytes = 0
        self._raw_pending_bytes = 0
        # Queued upload jobs by future → (args, kind). Lets discard_task() cancel them and clean their spill files.
        self._jobs: dict = {}
        # Triggers handed to a camera thread and not yet reported back to the collector (queued + in capture).
        self._capture_inflight = 0

        # Backend-driven task (see capture_task.py). Every trigger is stamped with the active task id and a
        # generation at receipt, so frames still in flight keep the right id however long they take to upload.
        self._task_lock = threading.Lock()
        self._task_id: str | None = None
        self._gen = 0            # generation of the active (or last) task; bumps on every begin_task
        self._discard_gen = 0    # events of generation <= this (and > 0) are dropped instead of uploaded
        self._task_counters: dict[str, int] = self._zero_task_counters()
        self._event_callbacks: list = []
        self._ack_waiters: dict[str, threading.Event] = {}
        self._retry_wake = threading.Event()
        self._disk_cache: dict = {}

        self._trigger_gate_open = True  # closed by the task manager: triggers are then ignored, not captured

        self._stats = {
            "triggers_gated": 0,
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
            "connect_count": 0,
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
            self._retry_wake.clear()
            with self._pool_counter_lock:
                self._capture_inflight = sum(q.qsize() for q in self._camera_queues.values())
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
            self._retry_wake.set()

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

    def note_count_reset(self) -> None:
        """Mirror a reset_count command locally so the UI shows 0 now, not after the next heartbeat."""
        with self._state_lock:
            self._arduino_state["encoder_count"] = 0
            self._arduino_state["speed_cms"] = 0.0

    # ── Backend-driven task support (driven by capture_task.TaskManager) ───────

    @staticmethod
    def _zero_task_counters() -> dict[str, int]:
        return {"triggers": 0, "captured": 0, "capture_failed": 0, "uploaded": 0, "upload_failed": 0, "discarded": 0}

    def add_event_listener(self, callback) -> None:
        """Register callback({"type", "data"}) for pipeline events (capture/upload failures, Arduino link).

        Called from capture/upload/serial threads, so it must return quickly and never raise.
        """
        self._event_callbacks.append(callback)

    def _emit(self, event_type: str, **data) -> None:
        for cb in list(self._event_callbacks):
            try:
                cb({"type": event_type, "data": data})
            except Exception:
                logger.exception("trigger_event_callback_failed", event_type=event_type)

    def set_trigger_gate(self, open_: bool) -> None:
        """Close the gate to make the listener ignore Arduino triggers (software backstop for set_trigger_enabled).

        An Arduino that resets (serial reconnect) comes back with triggers on and a zeroed encoder; until the disable
        command lands it can fire frames with a wrong distance. With the gate closed those are dropped on receipt.
        """
        self._trigger_gate_open = bool(open_)

    def begin_task(self, task_id: str) -> None:
        """Stamp every trigger from now on with task_id and zero the per-task counters."""
        with self._task_lock:
            self._gen += 1
            self._task_id = task_id
            self._task_counters = self._zero_task_counters()

    def end_task(self) -> None:
        """Stop stamping new triggers (frames already stamped keep their id)."""
        with self._task_lock:
            self._task_id = None

    def restore_task_counters(self, counters: dict) -> None:
        with self._task_lock:
            self._task_counters = {**self._zero_task_counters(), **{k: int(v) for k, v in counters.items() if k in self._task_counters}}

    def task_counters(self) -> dict[str, int]:
        with self._task_lock:
            return dict(self._task_counters)

    def _task_bump(self, event: dict, key: str, n: int = 1) -> None:
        """Count against the active task, only when the event belongs to it."""
        tid = event.get("task_id")
        if not tid:
            return
        with self._task_lock:
            if tid == self._task_id:
                self._task_counters[key] += n

    def _discarded(self, event: dict) -> bool:
        gen = event.get("_gen", 0)
        return gen > 0 and gen <= self._discard_gen

    def send_command_wait_ack(self, cmd: dict, timeout: float = 3.0) -> bool:
        """send_command() and block until the Arduino acks it. False on timeout."""
        name = cmd["cmd"]
        waiter = threading.Event()
        self._ack_waiters[name] = waiter
        try:
            self.send_command(cmd)
            return waiter.wait(timeout)
        finally:
            self._ack_waiters.pop(name, None)

    def kick_disk_retry(self) -> None:
        """Retry disk-backed uploads now instead of at the next 60 s tick."""
        self._retry_wake.set()

    def _task_disk_files(self, task_id: str | None, max_age_s: float = 0.75) -> list[Path]:
        """Disk-backed (failed, awaiting retry) images that belong to task_id, found via their sidecars."""
        if not task_id:
            return []
        now = time.monotonic()
        cached = self._disk_cache.get(task_id)
        if cached is not None and now - cached[0] < max_age_s:
            return cached[1]
        save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
        found: list[Path] = []
        if save_dir.exists():
            for sidecar in save_dir.glob("*.jpg.json"):
                try:
                    if json.loads(sidecar.read_text()).get("task_id") != task_id:
                        continue
                except Exception:
                    continue
                image = sidecar.with_suffix("")
                if image.exists():
                    found.append(image)
        self._disk_cache[task_id] = (now, found)
        return found

    def task_pending(self, task_id: str | None) -> dict:
        """Work still outstanding for the task: what a graceful stop has to wait for."""
        with self._pool_counter_lock:
            capture, stitch, raw = self._capture_inflight, self._stitch_pending, self._raw_pending
        disk = len(self._task_disk_files(task_id))
        return {"capture": capture, "stitch": stitch, "raw": raw, "disk_retry": disk,
                "total": capture + stitch + raw + disk}

    def memory_pending(self) -> int:
        """Capture + upload work held in memory (excludes disk-backed retries)."""
        with self._pool_counter_lock:
            return self._capture_inflight + self._stitch_pending + self._raw_pending

    def discard_task(self, task_id: str | None) -> dict:
        """Drop everything not yet uploaded for the active task. Returns what was dropped.

        Queued triggers and not-yet-started upload jobs are removed; a job already running finishes its HTTP
        request but skips every further upload and its local-save fallback (see _discarded()). Disk-backed
        files that belong to the task are deleted.
        """
        with self._task_lock:
            self._discard_gen = self._gen
        dropped = {"queued_captures": 0, "upload_jobs": 0, "aborted_jobs": 0, "disk_files": 0, "files": []}

        for q in self._camera_queues.values():
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    dropped["queued_captures"] += 1
                    with self._pool_counter_lock:
                        self._capture_inflight = max(0, self._capture_inflight - 1)

        with self._pool_counter_lock:
            jobs = list(self._jobs.items())
        for fut, (args, _kind) in jobs:
            if fut.cancel():  # runs the done-callback, which fixes the counters
                dropped["upload_jobs"] += 1
                self._unlink_spilled(args)
            elif not fut.done():
                # Already running: it cannot be recalled, but it stops before its next attempt (see _discarded()).
                dropped["aborted_jobs"] += 1

        for image in self._task_disk_files(task_id, max_age_s=0):
            try:
                image.unlink(missing_ok=True)
                image.with_suffix(".jpg.json").unlink(missing_ok=True)
                dropped["disk_files"] += 1
                if len(dropped["files"]) < 100:
                    dropped["files"].append(image.name)
            except Exception as exc:
                logger.warning("task_discard_file_failed", filename=image.name, error=str(exc))
        self._disk_cache.clear()

        total = dropped["queued_captures"] + dropped["upload_jobs"] + dropped["aborted_jobs"] + dropped["disk_files"]
        if task_id:
            with self._task_lock:
                if task_id == self._task_id:
                    self._task_counters["discarded"] += total
        logger.info("task_discarded", task_id=task_id, **{k: v for k, v in dropped.items() if k != "files"})
        return dropped

    @staticmethod
    def _unlink_spilled(args: tuple) -> None:
        """Delete spill files referenced by a job's {cam_id: (bytes | Path, serial, timing)} argument."""
        for arg in args:
            if isinstance(arg, dict):
                for value in arg.values():
                    if isinstance(value, tuple) and value and isinstance(value[0], Path):
                        try:
                            value[0].unlink(missing_ok=True)
                        except Exception:
                            pass

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
            "active_style": self._load_file_config().get("active_style"),
            **stats_snapshot,
            **state,
            "distance_mm": distance_mm_from_count(state.get("encoder_count", 0), self._counts_per_cm()),
        }

    def queue_depths(self) -> dict:
        """Return a snapshot of backlog depths for all pipeline queues."""
        camera_queues = {cam_id: q.qsize() for cam_id, q in self._camera_queues.items()}
        camera_queue_maxsize = next(iter(self._camera_queues.values())).maxsize if self._camera_queues else 0
        with self._pool_counter_lock:
            stitch_pending = self._stitch_pending
            raw_pending = self._raw_pending
            stitch_pending_mb = round(self._stitch_pending_bytes / 1024 / 1024, 1)
            raw_pending_mb = round(self._raw_pending_bytes / 1024 / 1024, 1)
            capture_inflight = self._capture_inflight
        with self._collector._lock:
            collector_pending = len(self._collector._pending)
        disk_retry, disk_spill = self._disk_file_counts()
        return {
            "camera_queues": camera_queues,
            "camera_queue_maxsize": camera_queue_maxsize,
            "capture_inflight": capture_inflight,
            "collector_pending": collector_pending,
            "stitch_pending": stitch_pending,
            "stitch_pending_mb": stitch_pending_mb,
            "raw_pending": raw_pending,
            "raw_pending_mb": raw_pending_mb,
            "disk_retry": disk_retry,
            "disk_spill": disk_spill,
            # Everything still to be uploaded. Spill files are excluded: each belongs to a queued job already counted.
            "pending_total": capture_inflight + stitch_pending + raw_pending + disk_retry,
        }

    def _disk_file_counts(self, max_age_s: float = 1.0) -> tuple[int, int]:
        """(disk_retry, disk_spill) file counts, cached briefly: this is polled by every stream client."""
        now = time.monotonic()
        cached = getattr(self, "_disk_counts_cache", None)
        if cached is not None and now - cached[0] < max_age_s:
            return cached[1]
        save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
        disk_retry = sum(1 for _ in save_dir.glob("*.jpg")) if save_dir.exists() else 0
        spill_dir = config.CAPTURE_TMP_DIR / "hw_trigger" / "spill"
        disk_spill = sum(1 for _ in spill_dir.glob("*.jpg")) if spill_dir.exists() else 0
        self._disk_counts_cache = (now, (disk_retry, disk_spill))
        return disk_retry, disk_spill

    # ── Private ───────────────────────────────────────────────────────────────

    def _spill_raw_captures_to_disk(
        self,
        raw_captures: dict,
        ts_ms: int,
        prefix: str,
    ) -> dict:
        """Write raw JPEG bytes to temp files and return a dict with Paths instead of bytes.

        Called when a memory budget is exceeded so in-flight jobs hold file paths
        rather than large byte buffers. Workers read and delete the files on use.
        """
        spill_dir = config.CAPTURE_TMP_DIR / "hw_trigger" / "spill"
        spill_dir.mkdir(parents=True, exist_ok=True)
        spilled: dict = {}
        for cam_id, (jpeg_bytes, serial, timing) in raw_captures.items():
            path = spill_dir / f"{prefix}_{ts_ms}_{cam_id}.jpg"
            path.write_bytes(jpeg_bytes)
            spilled[cam_id] = (path, serial, timing)
        return spilled

    def _stitch_submit(self, fn, *args, byte_count: int = 0) -> None:
        self._pool_submit("stitch", fn, args, byte_count)

    def _raw_submit(self, fn, *args, byte_count: int = 0) -> None:
        self._pool_submit("raw", fn, args, byte_count)

    def _pool_submit(self, kind: str, fn, args: tuple, byte_count: int) -> None:
        """Submit a job and keep the pending counters exact.

        The counters are released by a done-callback rather than inside the job, because a job cancelled
        before it starts (pool shutdown, discard_task) never runs and would otherwise leave them stuck high.
        """
        with self._pool_counter_lock:
            pool = self._stitch_pool if kind == "stitch" else self._raw_pool
            if pool is None:
                return
            if kind == "stitch":
                self._stitch_pending += 1
                self._stitch_pending_bytes += byte_count
            else:
                self._raw_pending += 1
                self._raw_pending_bytes += byte_count

        def _release(fut) -> None:
            with self._pool_counter_lock:
                self._jobs.pop(fut, None)
                if kind == "stitch":
                    self._stitch_pending = max(0, self._stitch_pending - 1)
                    self._stitch_pending_bytes = max(0, self._stitch_pending_bytes - byte_count)
                else:
                    self._raw_pending = max(0, self._raw_pending - 1)
                    self._raw_pending_bytes = max(0, self._raw_pending_bytes - byte_count)

        try:
            fut = pool.submit(fn, *args)
        except RuntimeError:  # pool was shut down between the check above and now
            with self._pool_counter_lock:
                if kind == "stitch":
                    self._stitch_pending = max(0, self._stitch_pending - 1)
                    self._stitch_pending_bytes = max(0, self._stitch_pending_bytes - byte_count)
                else:
                    self._raw_pending = max(0, self._raw_pending - 1)
                    self._raw_pending_bytes = max(0, self._raw_pending_bytes - byte_count)
            return
        with self._pool_counter_lock:
            if not fut.done():  # a job that already finished has released itself; don't leave an entry behind
                self._jobs[fut] = (args, kind)
        fut.add_done_callback(_release)

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
                    if is_serial_url(port):
                        # pyserial URL handlers (socket://...) are only honoured in mock mode, where
                        # mocks/fake_arduino.py stands in for the board. On real deployments a URL is never opened.
                        if not url_ports_allowed():
                            raise serial.serialutil.SerialException(f"serial URL {port!r} refused: not in mock mode")
                        ser = serial.serial_for_url(port, baudrate=baud, timeout=0.1)
                    else:
                        ser = serial.Serial(port, baud, timeout=0.1)
                    with self._state_lock:
                        self._arduino_state["serial_connected"] = True
                        self._arduino_state["connect_count"] = self._arduino_state.get("connect_count", 0) + 1
                    logger.info("serial_port_opened", port=port, baud=baud)
                    self._emit("arduino.connected")
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
                    # Don't leave the last belt speed on screen as if it were still live.
                    was_connected = self._arduino_state["serial_connected"]
                    self._arduino_state["serial_connected"] = False
                    self._arduino_state["speed_cms"] = 0.0
                if was_connected:
                    self._emit("arduino.disconnected", error=str(exc))
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

            if not self._trigger_gate_open:
                with self._stats_lock:
                    self._stats["triggers_gated"] += 1
                logger.warning("hw_trigger_ignored_gate_closed", count=event.get("count"), trigger=event.get("trigger"))
                return

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
            with self._task_lock:
                if self._task_id:
                    event["task_id"] = self._task_id
                    event["_gen"] = self._gen
                    self._task_counters["triggers"] += 1
                else:
                    event["_gen"] = 0
            for cam_id, q in self._camera_queues.items():
                try:
                    with self._pool_counter_lock:
                        self._capture_inflight += 1
                    q.put_nowait(event)
                except queue.Full:
                    with self._pool_counter_lock:
                        self._capture_inflight = max(0, self._capture_inflight - 1)
                    logger.warning(
                        "hw_trigger_queue_full_dropped",
                        camera_id=cam_id,
                        queue_size=q.maxsize,
                        trigger=event,
                    )

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
            cmd = event.get("cmd")
            if cmd == "reset_count" and event.get("ok"):
                # Serial is ordered: every message after this ack is post-reset, so this is the real zero.
                self.note_count_reset()
            waiter = self._ack_waiters.get(cmd)
            if waiter is not None and event.get("ok"):
                waiter.set()

        elif msg_type == "startup":
            logger.info("arduino_startup", msg=event.get("msg"))

    def _counts_per_cm(self) -> float | None:
        """Encoder scale: live Arduino config first, then the saved file config, then derived from the wheel."""
        with self._state_lock:
            live = self._arduino_state.get("arduino_config", {}).get("counts_per_cm")
        if live:
            return float(live)
        cfg = self._load_file_config()
        if cfg.get("counts_per_cm"):
            return float(cfg["counts_per_cm"])
        return compute_arduino_params(
            float(cfg.get("wheel_diameter_mm", PHYSICAL_DEFAULTS["wheel_diameter_mm"])),
            int(cfg.get("encoder_ppr", PHYSICAL_DEFAULTS["encoder_ppr"])),
            float(cfg.get("capture_interval_mm", PHYSICAL_DEFAULTS["capture_interval_mm"])),
        )["counts_per_cm"]

    def _exif_extra(self, event: dict) -> dict:
        """Per-trigger fields written into the image EXIF so reports can tell where along the roll a frame is."""
        extra: dict = {}
        if event.get("task_id"):
            extra["task_id"] = event["task_id"]
        if event.get("trigger") is not None:
            extra["trigger_number"] = event["trigger"]
        count = event.get("count")
        if count is not None:
            extra["encoder_count"] = count
            distance = distance_mm_from_count(count, self._counts_per_cm())
            if distance is not None:
                extra["distance_mm"] = distance
        return extra

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

            try:
                self._process_trigger(cam_id, cam, event, tmp_dir)
            finally:
                # Only now is the trigger handed on (to the collector), so pending never dips to zero mid-flight.
                with self._pool_counter_lock:
                    self._capture_inflight = max(0, self._capture_inflight - 1)

    def _process_trigger(self, cam_id: int, cam: "MindVisionCamera", event: dict, tmp_dir: Path) -> None:
        """Capture one frame for one trigger and report it (or the failure) to the collector."""
        if self._discarded(event):
            self._collector.failure(event, cam_id)
            return

        received_at = event.get("_received_at")
        queue_ms: float | None = None
        if received_at is not None:
            queue_age = time.monotonic() - received_at
            queue_ms = round(queue_age * 1000, 2)
            max_age = float(runtime_config.get("hw_trigger.max_queue_age_s", 5.0))
            if queue_age > max_age:
                logger.warning(
                    "hw_trigger_dropped_stale",
                    camera_id=cam_id,
                    queue_age_s=round(queue_age, 2),
                    trigger=event,
                )
                self._task_bump(event, "capture_failed")
                self._emit("capture.failed", camera_id=cam_id, trigger=event.get("trigger"), error="dropped: stale")
                self._collector.failure(event, cam_id)
                return

        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=tmp_dir) as td:
                image_path, capture_metrics = cam.capture_image(output_folder=Path(td), extra_exif=self._exif_extra(event))
                jpeg_bytes = image_path.read_bytes()
            serial = cam.serial_number or str(cam_id)
            with self._stats_lock:
                self._stats["captures_ok"] += 1
            self._task_bump(event, "captured")
            logger.debug("hw_trigger_captured", camera_id=cam_id, trigger=event)
            timing = {
                "queue_ms": queue_ms,
                "capture_ms": round(capture_metrics.capture_duration_ms, 2),
            }
            self._collector.add_frame(event, cam_id, jpeg_bytes, serial, timing)
        except Exception as exc:
            with self._stats_lock:
                self._stats["captures_failed"] += 1
            self._task_bump(event, "capture_failed")
            self._emit("capture.failed", camera_id=cam_id, trigger=event.get("trigger"), error=str(exc))
            logger.warning("hw_trigger_capture_failed", camera_id=cam_id, error=str(exc))
            self._collector.failure(event, cam_id)

    def _on_all_captured(
        self,
        event: dict,
        raw_captures: dict[int, tuple[bytes, str, dict]],
        ts_ms: int,
    ) -> None:
        """Called by _TriggerCollector once every camera has reported for a trigger."""
        total_bytes = sum(len(data) for data, _, _ in raw_captures.values())
        budget_bytes = int(runtime_config.get("hw_trigger.stitch_memory_budget_mb", 1024)) * 1024 * 1024

        with self._pool_counter_lock:
            pending_bytes = self._stitch_pending_bytes

        if pending_bytes + total_bytes > budget_bytes:
            logger.warning(
                "hw_trigger_stitch_memory_budget_exceeded",
                trigger=event,
                pending_mb=round(pending_bytes / 1024 / 1024, 1),
                new_mb=round(total_bytes / 1024 / 1024, 1),
                budget_mb=budget_bytes // 1024 // 1024,
            )
            raw_captures = self._spill_raw_captures_to_disk(raw_captures, ts_ms, "stitch")
            total_bytes = 0  # spilled captures no longer held in RAM

        self._stitch_submit(self._stitch_and_upload, event, raw_captures, ts_ms, byte_count=total_bytes)

    def _stitch_and_upload(
        self,
        event: dict,
        raw_captures: dict[int, tuple["bytes | Path", str, dict]],
        ts_ms: int,
    ) -> None:
        """Upload the primary capture(s) immediately. Debug raw uploads go to the lower-priority pool.

        When hw_trigger.use_stitch is true, computes a stitched composite and uploads
        that. When false (default), skips stitching entirely and uploads each camera's
        raw image individually, tagged with its camera_id — this is the primary upload
        in that mode, not the debug raw-upload pool below.

        Runs in _stitch_pool (dedicated workers) so these primary uploads are never
        queued behind pending debug raw uploads from earlier triggers.
        Images stay in memory; disk is only written if the upload fails completely.
        raw_captures values may hold either bytes (normal path) or a Path (spilled to
        disk when the memory budget was exceeded) — both are handled transparently.
        """
        import cv2
        import numpy as np

        # Materialize any captures that were spilled to disk before processing.
        materialized: dict[int, tuple[bytes, str, dict]] = {}
        for cam_id, (data, serial, timing) in raw_captures.items():
            if isinstance(data, Path):
                try:
                    materialized[cam_id] = (data.read_bytes(), serial, timing)
                finally:
                    try:
                        data.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                materialized[cam_id] = (data, serial, timing)
        raw_captures = materialized

        if self._discarded(event):  # the task was force-stopped while this job sat in the queue
            return

        # Timing payload sent to the server with every upload from this trigger.
        # Stage keys are always present so the schema is stable regardless of
        # which stages actually ran (decode/stitch/encode are null unless
        # hw_trigger.use_stitch is on).
        timings: dict = {
            "trigger": event.get("trigger"),
            "cameras": {cam_id: t for cam_id, (_, _, t) in raw_captures.items()},
            "decode_ms": None,
            "stitch_ms": None,
            "encode_ms": None,
        }

        use_stitch = _get_use_stitch()

        stitch_filename = f"{ts_ms}_stitch.jpg"
        stitch_bytes: bytes | None = None

        if use_stitch and len(raw_captures) >= 2 and self._load_calibration and self._stitch_frames:
            cal = self._load_calibration()
            if cal:
                import time as _time
                frames: dict[int, np.ndarray] = {}
                t_decode = 0.0
                for cam_id, (jpeg_bytes, _, _t) in raw_captures.items():
                    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    _t0 = _time.perf_counter()
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    t_decode += _time.perf_counter() - _t0
                    if frame is not None:
                        frames[cam_id] = frame
                timings["decode_ms"] = round(t_decode * 1000, 2)

                _t0 = _time.perf_counter()
                stitched = self._stitch_frames(frames, cal) if frames else None
                t_stitch = _time.perf_counter() - _t0
                timings["stitch_ms"] = round(t_stitch * 1000, 2)

                if stitched is not None:
                    _t0 = _time.perf_counter()
                    ok_enc, buf = cv2.imencode(".jpg", stitched, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    t_encode = _time.perf_counter() - _t0
                    if ok_enc:
                        stitch_bytes = buf.tobytes()
                        timings["encode_ms"] = round(t_encode * 1000, 2)
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

        if use_stitch:
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
                stitch_ok = _upload_image(stitch_bytes, event, url=url, filename=stitch_filename, ts_ms=ts_ms, timing=timings,
                                          abort=lambda: self._discarded(event))
                self._record_upload(event, stitch_ok, stitch_filename)

            # Save locally if explicitly requested OR as fallback when upload failed.
            if not stitch_ok and _get_save_local() and not self._discarded(event):
                _save_image_locally(stitch_bytes, event, filename=stitch_filename, ts_ms=ts_ms, timing=timings)
                if url:
                    logger.warning(
                        "hw_trigger_stitch_fallback_local",
                        filename=stitch_filename,
                        trigger=event,
                    )
        else:
            # === PER-CAMERA UPLOAD — primary path when use_stitch is disabled ===
            # Runs synchronously here in _stitch_pool, i.e. at the same priority as the
            # stitch upload above, so it is never queued behind the debug raw pool.
            # Each image is tagged with its camera_id instead of being combined.
            url = _get_destination_url()
            if url:
                for cam_id, (jpeg_bytes, _serial, _t) in raw_captures.items():
                    if self._discarded(event):
                        break
                    cam_filename = f"{ts_ms}_cam{cam_id}.jpg"
                    ok = _upload_image(jpeg_bytes, event, url=url, filename=cam_filename, camera_id=cam_id, ts_ms=ts_ms, timing=timings,
                                       abort=lambda: self._discarded(event))
                    self._record_upload(event, ok, cam_filename)
                    if not ok and _get_save_local() and not self._discarded(event):
                        _save_image_locally(jpeg_bytes, event, filename=cam_filename, ts_ms=ts_ms, timing=timings)
                        logger.warning(
                            "hw_trigger_camera_fallback_local",
                            filename=cam_filename,
                            trigger=event,
                        )

        # === RAW UPLOADS — debug only, enqueued after the primary upload is done ===
        # Submitted to the single-worker raw pool so they drain slowly in the
        # background and never contend with stitch uploads for network bandwidth.
        raw_url = _get_raw_destination_url() if _get_send_raw_images() else None
        if raw_url:
            raw_bytes = sum(len(data) for data, _, _ in raw_captures.values())
            raw_budget_bytes = int(runtime_config.get("hw_trigger.raw_memory_budget_mb", 2048)) * 1024 * 1024
            with self._pool_counter_lock:
                raw_pending_bytes = self._raw_pending_bytes
            if raw_pending_bytes + raw_bytes > raw_budget_bytes:
                logger.warning(
                    "hw_trigger_raw_memory_budget_exceeded",
                    trigger=event,
                    pending_mb=round(raw_pending_bytes / 1024 / 1024, 1),
                    new_mb=round(raw_bytes / 1024 / 1024, 1),
                    budget_mb=raw_budget_bytes // 1024 // 1024,
                )
                raw_captures_for_pool = self._spill_raw_captures_to_disk(raw_captures, ts_ms, "raw")
                raw_bytes = 0
            else:
                raw_captures_for_pool = raw_captures
            self._raw_submit(self._upload_raw_captures, raw_captures_for_pool, event, ts_ms, raw_url, byte_count=raw_bytes)

    def _record_upload(self, event: dict, ok: bool, filename: str) -> None:
        """Count a primary upload (global stats and the active task) and announce a failure."""
        if self._discarded(event):  # abandoned by a force-stop, neither a success nor a failure
            return
        with self._stats_lock:
            self._stats["uploads_ok" if ok else "uploads_failed"] += 1
        self._task_bump(event, "uploaded" if ok else "upload_failed")
        if not ok:
            self._emit("upload.failed", filename=filename, trigger=event.get("trigger"))

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
        raw_captures: dict[int, tuple["bytes | Path", str, dict]],
        event: dict,
        ts_ms: int,
        raw_url: str,
    ) -> None:
        """Upload individual camera images. Debug only — runs in the single-worker raw pool.

        Yields to stitch uploads before starting: if the stitch pool has any
        pending work this method blocks until it drains, so raw traffic never
        competes with stitch for bandwidth.
        raw_captures values may hold either bytes or a Path (spilled to disk).
        """
        self._wait_for_stitch_drain()
        for cam_id, (data, serial, timing) in raw_captures.items():
            if self._discarded(event):
                self._unlink_spilled((raw_captures,))
                return
            if isinstance(data, Path):
                try:
                    jpeg_bytes = data.read_bytes()
                finally:
                    try:
                        data.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                jpeg_bytes = data
            filename = f"{ts_ms}_{serial}.jpg"
            ok = _upload_image(jpeg_bytes, event, url=raw_url, filename=filename, is_raw=True, ts_ms=ts_ms, timing=timing,
                               abort=lambda: self._discarded(event))
            with self._stats_lock:
                if ok:
                    self._stats["uploads_ok"] += 1
                else:
                    self._stats["uploads_failed"] += 1
                    if not self._discarded(event):
                        _save_image_locally(jpeg_bytes, event, filename=filename, ts_ms=ts_ms, timing=timing)
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
        while True:
            self._retry_wake.wait(timeout=60)
            self._retry_wake.clear()
            if self._stop_event.is_set():
                return
            save_dir = config.HW_TRIGGER_LOCAL_SAVE_DIR
            if not save_dir.exists():
                continue

            stitch_files = sorted(save_dir.glob("*_stitch.jpg"))
            raw_files = [f for f in sorted(save_dir.glob("*.jpg")) if not f.name.endswith("_stitch.jpg")]

            for f in stitch_files + raw_files:
                if self._stop_event.is_set():
                    return
                is_stitch = f.name.endswith("_stitch.jpg")
                # Per-camera primary uploads ("<ts>_cam<N>.jpg") go to the main destination with their camera id,
                # exactly like the upload that failed. Only debug raw copies use the raw destination.
                cam_match = _CAM_FILE_RE.search(f.name)
                camera_id = int(cam_match.group(1)) if cam_match else None
                url = _get_destination_url() if (is_stitch or camera_id is not None) else _get_raw_destination_url()
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
                    retry_ts_ms = trigger_event.pop("ts_ms", None)
                    retry_timing = trigger_event.pop("timing", None)
                    image_bytes = f.read_bytes()
                    ok = _upload_image(image_bytes, trigger_event, url=url, filename=f.name, camera_id=camera_id,
                                       ts_ms=retry_ts_ms, timing=retry_timing)
                    if ok:
                        f.unlink(missing_ok=True)
                        sidecar.unlink(missing_ok=True)
                        self._disk_cache.clear()
                        self._task_bump(trigger_event, "uploaded")
                        logger.info("hw_trigger_disk_retry_ok", filename=f.name, url=url)
                except Exception as exc:
                    logger.warning("hw_trigger_disk_retry_error", filename=f.name, error=str(exc))
