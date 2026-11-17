"""Backend-driven capture task: start / stop / force-stop, persisted across restarts.

The backend starts a task with an id; from then on every trigger is stamped with that id (EXIF and upload
payload) until the task is stopped and its uploads have drained. See docs/backend-controlled-capture.md.

    idle ──start──▶ running ──stop──▶ draining ──queue empty──▶ idle
                       │                 ▲
        Pi restart     ▼                 │ stop
                  interrupted ───────────┘          (force stop from any non-idle state discards what is pending)

`interrupted` is what a task becomes when the Pi restarts while it was running: triggers stay off, nothing new is
captured, and only the backend can close it (stop) — a new run can never inherit an unknown distance baseline.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import structlog

import config

logger = structlog.get_logger()

TASK_STATE_PATH = Path("data/task_state.json")

IDLE, RUNNING, DRAINING, INTERRUPTED = "idle", "running", "draining", "interrupted"

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

MAX_STOP_WAIT_S = 30.0


class TaskError(Exception):
    """A request the state machine refuses. Carries the HTTP status and a machine-readable code."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra

    def body(self) -> dict:
        return {"error": str(self), "code": self.code, **self.extra}


def validate_task_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TaskError(400, "invalid_task_id", "task_id must be a string")
    task_id = str(value)
    if not _TASK_ID_RE.match(task_id):
        raise TaskError(400, "invalid_task_id", "task_id must be 1-128 characters of A-Z a-z 0-9 . _ : -")
    return task_id


def _zero_counters() -> dict[str, int]:
    return {"triggers": 0, "captured": 0, "capture_failed": 0, "uploaded": 0, "upload_failed": 0, "discarded": 0}


class TaskManager:
    """Owns the task state machine. Drives the listener; never touches the capture hot path itself."""

    def __init__(
        self,
        listener,
        cameras: dict | None = None,
        state_path: Path | str = TASK_STATE_PATH,
        *,
        serial_connect_timeout_s: float = 10.0,
        ack_timeout_s: float = 3.0,
        discard_quiesce_s: float = 5.0,
        watch_interval_s: float = 0.5,
        drain_timeout_s: float | None = None,
    ) -> None:
        self._listener = listener
        self._drain_timeout_s = float(config.TASK_DRAIN_TIMEOUT_S if drain_timeout_s is None else drain_timeout_s)
        self._cameras = cameras or {}
        self._state_path = Path(state_path)
        self._serial_connect_timeout_s = serial_connect_timeout_s
        self._ack_timeout_s = ack_timeout_s
        self._discard_quiesce_s = discard_quiesce_s
        self._watch_interval_s = watch_interval_s

        self._op_lock = threading.RLock()     # serialises start / stop / finish
        self._state_lock = threading.Lock()   # guards self._s for readers
        self._persist_lock = threading.Lock() # one writer of the state file at a time
        self._s: dict[str, Any] = {
            "task_id": None,
            "state": IDLE,
            "started_at": None,
            "stopped_at": None,
            "finished_at": None,
            "counters": _zero_counters(),
            "connect_base": 0,
            "last": None,
        }
        self._drain_since = time.monotonic()  # when the current drain began (stop, or boot for a resumed drain)
        self._tagging = False  # True while this process is stamping triggers for the task
        self._subscribers: list[Callable[[dict], None]] = []
        self._watcher: threading.Thread | None = None
        self._watcher_lock = threading.Lock()

        listener.add_event_listener(self._on_listener_event)

    # ── Subscribers (the websocket hub) ───────────────────────────────────────

    def add_subscriber(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _publish(self, event_type: str, **data: Any) -> None:
        event = {"type": event_type, "data": data, "ts": int(time.time() * 1000)}
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:
                logger.exception("task_subscriber_failed", event_type=event_type)

    def _on_listener_event(self, event: dict) -> None:
        if event["type"] == "arduino.connected":
            self._on_arduino_connected()
        self._publish(event["type"], **event["data"])

    def _on_arduino_connected(self) -> None:
        """The serial link (re)opened. While running that resets the Arduino's encoder, so the distance baseline is
        gone: stop accepting triggers and mark the task interrupted. Runs on the serial thread.

        Deliberately takes no op lock: start/stop hold it while waiting for an Arduino ack, and the ack arrives on
        this very thread. The RUNNING → INTERRUPTED switch is a compare-and-set under the state lock instead.
        """
        with self._state_lock:
            if self._s["state"] != RUNNING or not self._tagging:
                return  # first connect of a start in progress, idle, or already closed to triggers
            connects = self._listener.status().get("connect_count", 0)
            if connects <= self._s["connect_base"]:
                return
            self._s["state"] = INTERRUPTED
            task_id = self._s["task_id"]
        self._listener.set_trigger_gate(False)
        self._disable_triggers(wait=False)
        self._persist()
        logger.warning("task_interrupted_by_reconnect", task_id=task_id)
        self._publish("task.interrupted", task_id=task_id, reason="arduino_reconnected")

    # ── Reads ─────────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._s["state"]

    @property
    def task_id(self) -> str | None:
        with self._state_lock:
            return self._s["task_id"]

    def is_active(self) -> bool:
        return self.state != IDLE

    def guard(self, action: str) -> TaskError | None:
        """The error to answer a manual/legacy control with while a task owns the hardware, else None.

        action: "reset" | "stop" | "calibration" | "regular" — would disturb a running task, so blocked in any
        non-idle state; "hw_trigger" re-enables triggers, so it is only blocked while the task is closed to them.
        """
        state = self.state
        if state == IDLE:
            return None
        if action == "hw_trigger" and state == RUNNING:
            return None
        return TaskError(
            409, "task_active", f"Capture task {self.task_id!r} is {state}; use /api/task to stop it first",
            active_task_id=self.task_id, state=state,
        )

    def _counters(self) -> dict[str, int]:
        if self._tagging:
            return self._listener.task_counters()
        with self._state_lock:
            return dict(self._s["counters"])

    def status(self) -> dict:
        """The `task` block of the websocket state message."""
        with self._state_lock:
            s = dict(self._s)
        state = s["state"]
        pending = self._listener.task_pending(s["task_id"]) if state != IDLE else None
        reconnects = None
        if state != IDLE:
            reconnects = max(0, self._listener.status().get("connect_count", 0) - s["connect_base"])
        return {
            "id": s["task_id"],
            "state": state,
            "started_at": s["started_at"],
            "stopped_at": s["stopped_at"],
            "finished_at": s["finished_at"],
            "pending": pending,
            "arduino_reconnects": reconnects,
            **self._counters(),
            "last": s["last"],
        }

    def snapshot(self) -> dict:
        """Everything the monitor page shows, in one message. Same payload for GET /api/task/status and the socket."""
        st = self._listener.status()
        q = self._listener.queue_depths()
        return {
            "task": self.status(),
            "motion": {
                "speed_cms": st.get("speed_cms"),
                "distance_mm": st.get("distance_mm"),
                "encoder_count": st.get("encoder_count"),
            },
            "arduino": {
                "serial_connected": st.get("serial_connected"),
                "last_message_at": st.get("last_message_at"),
                "trigger_enabled": st.get("trigger_enabled"),
                "listener_running": st.get("running"),
                "port_present": os.path.exists(config.HW_TRIGGER_SERIAL_PORT),
                "simulator_running": st.get("simulator_running"),
            },
            "queues": {
                "camera": q["camera_queues"],
                "camera_maxsize": q["camera_queue_maxsize"],
                "capture_inflight": q["capture_inflight"],
                "collector": q["collector_pending"],
                "stitch": q["stitch_pending"],
                "stitch_mb": q["stitch_pending_mb"],
                "raw": q["raw_pending"],
                "raw_mb": q["raw_pending_mb"],
                "disk_retry": q["disk_retry"],
                "disk_spill": q["disk_spill"],
                "pending_total": q["pending_total"],
            },
            "totals": {
                "triggers": st.get("triggers_received"),
                "captures_ok": st.get("captures_ok"),
                "captures_failed": st.get("captures_failed"),
                "uploads_ok": st.get("uploads_ok"),
                "uploads_failed": st.get("uploads_failed"),
                "uptime_seconds": st.get("uptime_seconds"),
            },
            "active_style": st.get("active_style"),
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist(self, strict: bool = False) -> None:
        """Atomically write the state (temp file, fsync, rename) so a power cut leaves a whole file behind."""
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        try:
            # One writer at a time: the watcher, the serial thread (reconnect) and HTTP requests all persist, and
            # they share the temp file name. The snapshot is taken inside the lock so an older one cannot land last.
            with self._persist_lock:
                with self._state_lock:
                    payload = {k: self._s[k] for k in ("task_id", "state", "started_at", "stopped_at", "finished_at", "last")}
                    payload["counters"] = dict(self._s["counters"])
                if self._tagging:
                    payload["counters"] = self._listener.task_counters()
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                for attempt in range(4):
                    try:
                        os.replace(tmp, self._state_path)
                        break
                    except PermissionError:
                        # Windows refuses to replace a file another process (a reader, a scanner) has open; a
                        # moment later it works. A final persist that never lands would leave a finished task
                        # looking unfinished after a restart.
                        if attempt == 3:
                            raise
                        time.sleep(0.05)
        except OSError as exc:
            logger.error("task_state_persist_failed", path=str(self._state_path), error=str(exc))
            if strict:
                raise TaskError(500, "persist_failed", f"Could not save task state: {exc}") from exc

    def _load(self) -> dict | None:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.error("task_state_load_failed", path=str(self._state_path), error=str(exc))
            return None

    def recover(self) -> None:
        """Call once at boot, after the listener may have auto-started."""
        data = self._load()
        if not data or data.get("state") not in (IDLE, RUNNING, DRAINING, INTERRUPTED):
            return
        with self._op_lock:
            with self._state_lock:
                self._s.update(
                    task_id=data.get("task_id"),
                    state=data["state"],
                    started_at=data.get("started_at"),
                    stopped_at=data.get("stopped_at"),
                    finished_at=data.get("finished_at"),
                    last=data.get("last"),
                    counters={**_zero_counters(), **(data.get("counters") or {})},
                )
            if data["state"] == RUNNING:
                with self._state_lock:
                    self._s["state"] = INTERRUPTED
                self._persist()
                logger.warning("task_interrupted_by_restart", task_id=self.task_id)
                self._publish("task.interrupted", task_id=self.task_id, reason="pi_restart")
            if self.state != IDLE:
                self._drain_since = time.monotonic()  # a drain resumed after a restart gets its full timeout again
                self._listener.set_trigger_gate(False)
                self._disable_triggers(wait=False)
                self._ensure_watcher()
                logger.info("task_recovered", task_id=self.task_id, state=self.state)

    # ── Commands ──────────────────────────────────────────────────────────────

    def start(self, task_id: Any) -> dict:
        task_id = validate_task_id(task_id)
        if config.is_capture_station():
            raise TaskError(409, "unsupported", "Capture tasks are not available on a qc/shading station")
        with self._op_lock:
            state, active = self.state, self.task_id
            if state == RUNNING:
                if active == task_id:
                    return self._start_body()  # a retried start is a no-op
                raise TaskError(409, "task_active", f"Task {active!r} is already running",
                                active_task_id=active, state=state)
            if state != IDLE:
                raise TaskError(409, state, f"Task {active!r} is {state}; stop it first",
                                active_task_id=active, state=state)

            undo = {"cameras": [], "listener": False}
            try:
                self._ensure_decoder(undo)
                self._disable_triggers(wait=False)
                # The Arduino must confirm the zero: until it does the encoder count (and so distance) is stale.
                if not self._listener.send_command_wait_ack({"cmd": "reset_count"}, self._ack_timeout_s):
                    raise TaskError(503, "reset_unconfirmed", "The Arduino did not confirm the distance reset")
            except Exception:
                self._revert_decoder(undo)
                raise

            counters = _zero_counters()
            now = time.time()
            self._listener.set_trigger_gate(True)
            self._listener.begin_task(task_id)
            self._tagging = True
            with self._state_lock:
                self._s.update(
                    task_id=task_id, state=RUNNING, started_at=now, stopped_at=None, finished_at=None,
                    counters=counters, connect_base=self._listener.status().get("connect_count", 0),
                )
            try:
                self._persist(strict=True)
            except TaskError:
                self._listener.end_task()
                self._tagging = False
                with self._state_lock:
                    self._s.update(task_id=None, state=IDLE, started_at=None)
                raise

            self._listener.set_trigger_state(True)
            self._listener.send_command({"cmd": "set_trigger_enabled", "value": True})
            logger.info("task_started", task_id=task_id)
            self._publish("task.started", task_id=task_id, started_at=now)
            self._ensure_watcher()
            return self._start_body()

    def _start_body(self) -> dict:
        with self._state_lock:
            return {"state": self._s["state"], "task_id": self._s["task_id"], "started_at": self._s["started_at"]}

    def stop(self, task_id: Any, force: bool = False, wait_s: float = 2.0) -> tuple[int, dict]:
        """Stop capturing. Returns (http_status, body): 200 idle, or 202 draining while uploads finish.

        Stopping never blocks on the network: wait_s only bounds how long we linger hoping the queue is already
        nearly empty. force discards whatever has not been uploaded.
        """
        task_id = validate_task_id(task_id)
        wait_s = max(0.0, min(float(wait_s), MAX_STOP_WAIT_S))
        with self._op_lock:
            state, active = self.state, self.task_id
            if state == IDLE:
                with self._state_lock:
                    last = self._s["last"]
                if last and last.get("task_id") == task_id:
                    return 200, {"state": IDLE, "task_id": task_id, "summary": last}
                raise TaskError(409, "task_not_active", f"Task {task_id!r} is not active", active_task_id=None)
            if task_id != active:
                raise TaskError(409, "task_mismatch", f"Task {active!r} is active, not {task_id!r}",
                                active_task_id=active, state=state)

            if state in (RUNNING, INTERRUPTED):
                # Wait for the Arduino to confirm: every trigger it fired before the disable arrives before the ack,
                # so by then they are all counted as in flight and the drain below cannot miss one.
                self._disable_triggers(wait=True)
                self._listener.set_trigger_gate(False)  # after the ack, so triggers fired before the stop still count
                self._drain_since = time.monotonic()
                with self._state_lock:
                    self._s.update(state=DRAINING, stopped_at=time.time())
                self._persist()
                logger.info("task_stopping", task_id=task_id, force=force)
                self._publish("task.stopping", task_id=task_id)
                self._listener.kick_disk_retry()
                self._ensure_watcher()

            if force:
                return 200, self._force_discard()

        deadline = time.monotonic() + wait_s
        while True:
            self._maybe_finish()
            if self.state == IDLE:
                with self._state_lock:
                    return 200, {"state": IDLE, "task_id": task_id, "summary": self._s["last"]}
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return 202, {"state": DRAINING, "task_id": task_id, "pending": self._listener.task_pending(task_id)}

    # ── Internals ─────────────────────────────────────────────────────────────

    def _ensure_decoder(self, undo: dict) -> None:
        """Cameras in hardware-trigger mode, listener running, Arduino reachable. Raises TaskError otherwise.

        Records in `undo` what this call changed ("cameras": switched camera ids, "listener": started it) so a
        failed start can put things back with _revert_decoder. Things that were already set up are left alone.
        """
        from camera.mindvision import CameraMode

        errors = {}
        for cam_id, cam in self._cameras.items():
            if getattr(cam, "mode", None) != CameraMode.HARDWARE_TRIGGER:
                try:
                    cam.set_mode(CameraMode.HARDWARE_TRIGGER)
                    undo["cameras"].append(cam_id)
                except Exception as exc:
                    errors[str(cam_id)] = str(exc)
        if errors:
            raise TaskError(503, "camera_mode_failed", "Failed to set hardware trigger mode", details=errors)

        if not self._listener.running:
            try:
                self._listener.start(port=config.HW_TRIGGER_SERIAL_PORT, baud=config.HW_TRIGGER_SERIAL_BAUD)
                undo["listener"] = True
            except Exception as exc:
                raise TaskError(500, "decoder_start_failed", f"Could not start the decoder: {exc}") from exc

        deadline = time.monotonic() + self._serial_connect_timeout_s
        while not self._listener.serial_connected:
            if time.monotonic() >= deadline:
                raise TaskError(503, "arduino_unavailable", "The Arduino is not connected")
            time.sleep(0.1)

    def _revert_decoder(self, undo: dict) -> None:
        """Undo what a failed start() changed: stop a listener we started, put switched cameras back to capture mode."""
        from camera.mindvision import CameraMode

        if undo["listener"]:
            try:
                self._listener.stop()
            except Exception:
                logger.exception("task_start_revert_listener_failed")
        for cam_id in undo["cameras"]:
            cam = self._cameras.get(cam_id)
            try:
                if cam is not None:
                    cam.set_mode(CameraMode.CAPTURE)
            except Exception as exc:
                logger.warning("task_start_revert_camera_failed", camera_id=cam_id, error=str(exc))

    def _disable_triggers(self, wait: bool) -> None:
        """Best effort: with the serial link down no trigger can fire anyway."""
        if not self._listener.running:
            return
        cmd = {"cmd": "set_trigger_enabled", "value": False}
        try:
            self._listener.set_trigger_state(False)
            if wait and self._listener.serial_connected:
                self._listener.send_command_wait_ack(cmd, self._ack_timeout_s)
            else:
                self._listener.send_command(cmd)
        except RuntimeError as exc:
            logger.warning("task_disable_triggers_failed", error=str(exc))

    def _maybe_finish(self) -> None:
        with self._op_lock:
            if self.state != DRAINING:
                return
            if self._listener.task_pending(self.task_id)["total"] > 0:
                return
            self._finish(forced=False)

    def _finish(self, forced: bool, dropped: dict | None = None, leftover: dict | None = None) -> dict:
        """draining → idle. Call with the op lock held.

        `leftover` is set when the drain timed out: what was still pending, which is NOT discarded (see _drain_timed_out).
        """
        counters = self._counters()
        self._listener.end_task()
        self._listener.set_trigger_gate(True)  # idle: the manual/legacy modes own the triggers again
        self._tagging = False
        with self._state_lock:
            summary = {
                "task_id": self._s["task_id"],
                "started_at": self._s["started_at"],
                "stopped_at": self._s["stopped_at"],
                "finished_at": time.time(),
                "forced": forced,
                "drain_timed_out": leftover is not None,
                **({"leftover": leftover} if leftover is not None else {}),
                **counters,
            }
            self._s.update(state=IDLE, finished_at=summary["finished_at"], counters=counters, last=summary)
        self._persist()
        logger.info("task_finished", **summary)
        if forced:
            self._publish("task.discarded", task_id=summary["task_id"], discarded=dropped, summary=summary)
        self._publish("task.finished", task_id=summary["task_id"], summary=summary)
        return summary

    def _drain_timed_out(self) -> None:
        """A stopped task has been draining longer than the timeout: close it, discarding nothing.

        Failed uploads stay on disk and the retry loop keeps working through them under their original task id; jobs
        still in flight finish normally. The summary says how much was left so the backend can reconcile.
        """
        with self._op_lock:
            if self.state != DRAINING:
                return
            if self._drain_timeout_s <= 0 or time.monotonic() - self._drain_since < self._drain_timeout_s:
                return
            pending = self._listener.task_pending(self.task_id)
            if pending["total"] == 0:
                self._finish(forced=False)
                return
            logger.warning("task_drain_timeout", task_id=self.task_id, timeout_s=self._drain_timeout_s, pending=pending)
            summary = self._finish(forced=False, leftover=pending)
            self._publish("task.drain_timeout", task_id=summary["task_id"], leftover=pending, summary=summary)

    def _force_discard(self) -> dict:
        """Drop everything not yet uploaded and go idle. Call with the op lock held."""
        dropped = self._listener.discard_task(self.task_id)
        # A request already on the wire cannot be recalled; give those a moment so the next task starts clean.
        deadline = time.monotonic() + self._discard_quiesce_s
        while self._listener.memory_pending() > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        still_running = self._listener.memory_pending()
        summary = self._finish(forced=True, dropped=dropped)
        return {"state": IDLE, "task_id": summary["task_id"], "summary": summary, "discarded": dropped,
                "still_running": still_running}

    # ── Watcher: finishes drains, keeps triggers off, checkpoints counters ────

    def _ensure_watcher(self) -> None:
        with self._watcher_lock:
            if self._watcher is not None and self._watcher.is_alive():
                return
            self._watcher = threading.Thread(target=self._watch, daemon=True, name="task-watch")
            self._watcher.start()

    def _watch(self) -> None:
        last_checkpoint = last_enforce = last_kick = time.monotonic()
        last_counters: dict | None = None
        while True:
            time.sleep(self._watch_interval_s)
            state = self.state
            if state == IDLE:
                return
            now = time.monotonic()
            try:
                if state == DRAINING:
                    self._maybe_finish()
                    self._drain_timed_out()
                    if now - last_kick >= 10:
                        last_kick = now
                        self._listener.kick_disk_retry()
                if state in (DRAINING, INTERRUPTED) and now - last_enforce >= 2:
                    # An Arduino that reset (serial reconnect) comes back with triggers on; the task is closed to them.
                    last_enforce = now
                    if self._listener.status().get("trigger_enabled"):
                        self._disable_triggers(wait=False)
                if state == RUNNING and now - last_checkpoint >= 5:
                    last_checkpoint = now
                    counters = self._counters()
                    if counters != last_counters:
                        last_counters = counters
                        self._persist()
            except Exception:
                logger.exception("task_watch_failed")
