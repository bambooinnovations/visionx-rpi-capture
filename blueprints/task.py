"""Backend-driven capture tasks: HTTP control under /api/task and a live websocket at /ws/task.

Commands are HTTP only; the websocket is read-only telemetry. See docs/backend-controlled-capture.md.

Websocket messages are JSON: {"v": 1, "type": ..., "seq": n, "ts": epoch_ms, "data": {...}}
  state  — full snapshot, on connect and then every 0.5 s while a task is active (1 s when idle). Losing one is
           harmless, so a reconnecting client needs no replay.
  event  — one-off notifications: data = {"name": "task.started" | ..., ...}
  ping   — liveness, every 10 s.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

import structlog
from flask import Blueprint, jsonify, request

from capture_task import MAX_STOP_WAIT_S, TaskError, TaskManager

logger = structlog.get_logger()

PROTOCOL_VERSION = 1
MAX_WS_CLIENTS = 4
STATE_INTERVAL_ACTIVE_S = 0.5
STATE_INTERVAL_IDLE_S = 1.0
PING_INTERVAL_S = 10.0
CLIENT_QUEUE_SIZE = 200
WS_TRY_AGAIN_LATER = 1013


class _Client:
    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=CLIENT_QUEUE_SIZE)

    def push(self, item: tuple[str, dict, int]) -> None:
        """Never blocks the producer (a capture or upload thread): a slow client loses its oldest message."""
        while True:
            try:
                self.q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass


class StreamHub:
    """Fans snapshots and events out to websocket clients. One broadcaster thread builds each snapshot once."""

    def __init__(self, manager: TaskManager, max_clients: int = MAX_WS_CLIENTS) -> None:
        self._manager = manager
        self._max_clients = max_clients
        self._clients: set[_Client] = set()
        self._lock = threading.Lock()
        self._broadcaster: threading.Thread | None = None
        manager.add_subscriber(self._on_event)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def register(self) -> _Client | None:
        with self._lock:
            if len(self._clients) >= self._max_clients:
                return None
            client = _Client()
            self._clients.add(client)
            if self._broadcaster is None or not self._broadcaster.is_alive():
                self._broadcaster = threading.Thread(target=self._broadcast, daemon=True, name="task-ws-broadcast")
                self._broadcaster.start()
            return client

    def unregister(self, client: _Client) -> None:
        with self._lock:
            self._clients.discard(client)

    def _fan_out(self, msg_type: str, data: dict, ts: int | None = None) -> None:
        item = (msg_type, data, ts if ts is not None else int(time.time() * 1000))
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.push(item)

    def _on_event(self, event: dict) -> None:
        self._fan_out("event", {"name": event["type"], **event["data"]}, event.get("ts"))

    def _broadcast(self) -> None:
        while True:
            with self._lock:
                if not self._clients:
                    self._broadcaster = None
                    return
            started = time.monotonic()
            try:
                snapshot = self._manager.snapshot()
                self._fan_out("state", snapshot)
                active = snapshot["task"]["state"] != "idle"
            except Exception:
                logger.exception("task_ws_snapshot_failed")
                active = False
            interval = STATE_INTERVAL_ACTIVE_S if active else STATE_INTERVAL_IDLE_S
            time.sleep(max(0.05, interval - (time.monotonic() - started)))


def _envelope(msg_type: str, seq: int, ts: int, data: dict) -> str:
    import json

    return json.dumps({"v": PROTOCOL_VERSION, "type": msg_type, "seq": seq, "ts": ts, "data": data})


def register_websocket(app, hub: StreamHub, manager: TaskManager) -> None:
    from flask_sock import Sock
    from simple_websocket import ConnectionClosed

    sock = Sock(app)

    @sock.route("/ws/task")
    def task_stream(ws):
        client = hub.register()
        if client is None:
            ws.close(reason=WS_TRY_AGAIN_LATER, message="too many clients")
            return
        seq = 0
        try:
            def send(msg_type: str, data: dict, ts: int | None = None) -> None:
                nonlocal seq
                seq += 1
                ws.send(_envelope(msg_type, seq, ts if ts is not None else int(time.time() * 1000), data))

            send("state", manager.snapshot())  # current immediately, not at the next tick
            last_ping = time.monotonic()
            while True:
                try:
                    msg_type, data, ts = client.q.get(timeout=0.5)
                    send(msg_type, data, ts)
                except queue.Empty:
                    pass
                if time.monotonic() - last_ping >= PING_INTERVAL_S:
                    last_ping = time.monotonic()
                    send("ping", {})
                ws.receive(timeout=0)  # drains incoming frames and raises ConnectionClosed once the peer is gone
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("task_ws_failed")
        finally:
            hub.unregister(client)


def create_blueprint(manager: TaskManager) -> Blueprint:
    bp = Blueprint("task", __name__, url_prefix="/api/task")

    def _error(exc: TaskError):
        return jsonify(exc.body()), exc.status

    @bp.route("/start", methods=["POST"])
    def task_start():
        """Start a capture task: reset distance, then stamp every capture with task_id.

        Body: {"task_id": "<id>"}. Starting the already-running id again is a no-op (200).
        409 if another task is running, draining or interrupted.
        """
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(manager.start(body.get("task_id")))
        except TaskError as exc:
            return _error(exc)

    @bp.route("/stop", methods=["POST"])
    def task_stop():
        """Stop capturing. 200 {"state": "idle"} once drained, 202 {"state": "draining"} while uploads finish.

        Body: {"task_id": "<id>", "force": false, "wait_s": 2}. force=true discards whatever is not uploaded
        yet and returns what was dropped. Idempotent.
        """
        body: dict[str, Any] = request.get_json(silent=True) or {}
        force = body.get("force", False)
        if not isinstance(force, bool):
            return jsonify({"error": "force must be a boolean", "code": "invalid_force"}), 400
        try:
            wait_s = float(body.get("wait_s", 2.0))
        except (TypeError, ValueError):
            return jsonify({"error": f"wait_s must be a number of seconds (max {MAX_STOP_WAIT_S:g})",
                            "code": "invalid_wait"}), 400
        try:
            status, payload = manager.stop(body.get("task_id"), force=force, wait_s=wait_s)
        except TaskError as exc:
            return _error(exc)
        return jsonify(payload), status

    @bp.route("/status", methods=["GET"])
    def task_status():
        """The same snapshot the websocket sends: task, motion, arduino, queues, totals."""
        return jsonify(manager.snapshot())

    return bp
