"""Fake Arduino: speaks the same JSON-lines protocol as arduino/camera_trigger/camera_trigger.ino.

The real board counts encoder pulses, fires a trigger every `trigger_interval` counts, and reports speed. This does
the same from a simulated belt speed, and serves it over TCP so the app can connect with the pyserial URL
`socket://127.0.0.1:<port>` (no virtual serial ports needed, works on Windows).

    python -m mocks.fake_arduino --port 7000 --speed 20

It mirrors the sketch's behaviour, including its quirks, so the host code is exercised against what the board really does:
  * emits {"type":"startup"} on connect, answers get_config with {"type":"config", ...}
  * every set_* command acks, then re-emits the config; invalid values ack with ok=false
  * reset_count zeroes the count, the last-trigger count and the speed
  * fire_trigger acks, then fires a trigger with source "serial" at the current count
  * triggers are distance-based (count - last_trigger_count >= trigger_interval) and re-sync while disabled
  * a speed heartbeat every speed_report_interval_ms, measured from raw movement

Fault injection for tests: set_speed(), drop_connection() (closes the client, like a cable pull), garbage lines.
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time

_TICK_S = 0.005


class FakeArduino:
    def __init__(self, port: int = 0, speed_cms: float = 0.0, host: str = "127.0.0.1") -> None:
        self.host = host
        # --- parameters (sketch defaults) ---
        self.trigger_interval = 1180
        self.counts_per_cm = 118.0
        self.pulse_width_ms = 20
        self.speed_report_interval_ms = 500
        self.trigger_enabled = True
        # --- state ---
        self.speed_cms = float(speed_cms)  # simulated belt speed (what the encoder would see)
        self.encoder_count = 0
        self._frac = 0.0
        self.trigger_number = 0
        self.last_trigger_count = 0
        self._last_speed_cms = 0.0
        self._last_speed_ms = 0.0
        self._last_speed_count = 0
        self._last_report_ms = 0.0
        # --- plumbing ---
        self._lock = threading.RLock()
        self._client: socket.socket | None = None
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(1)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.received_commands: list[dict] = []
        self.connections = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    @property
    def url(self) -> str:
        return f"socket://{self.host}:{self.port}"

    def start(self) -> "FakeArduino":
        for fn in (self._accept_loop, self._physics_loop):
            t = threading.Thread(target=fn, daemon=True, name=f"fake-arduino-{fn.__name__}")
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        self.drop_connection()
        try:
            self._server.close()
        except OSError:
            pass
        for t in self._threads:
            t.join(timeout=2)

    # ── test controls ────────────────────────────────────────────────────
    def set_speed(self, speed_cms: float) -> None:
        with self._lock:
            self.speed_cms = float(speed_cms)

    def drop_connection(self) -> None:
        """Close the current client like a pulled USB cable. A new client may reconnect afterwards."""
        with self._lock:
            c, self._client = self._client, None
        if c is not None:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def send_garbage(self, text: str = "\x00not json{{\r\n") -> None:
        self._write_raw(text.encode("latin-1"))

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._client is not None

    # ── output ───────────────────────────────────────────────────────────
    def _write_raw(self, data: bytes) -> None:
        with self._lock:
            c = self._client
        if c is None:
            return
        try:
            c.sendall(data)
        except OSError:
            self.drop_connection()

    def _emit(self, obj: dict) -> None:
        self._write_raw((json.dumps(obj, separators=(",", ":")) + "\n").encode("ascii"))

    def _emit_config(self) -> None:
        self._emit({
            "type": "config",
            "trigger_enabled": self.trigger_enabled,
            "trigger_interval": self.trigger_interval,
            "counts_per_cm": round(self.counts_per_cm, 2),
            "pulse_width_ms": self.pulse_width_ms,
            "speed_report_interval_ms": self.speed_report_interval_ms,
        })

    def _emit_ack(self, cmd: str, ok: bool) -> None:
        self._emit({"type": "ack", "cmd": cmd, "ok": ok})

    def _fire_trigger(self, source: str, count: int) -> None:
        self.trigger_number += 1
        self._emit({
            "type": "trigger", "source": source, "count": count,
            "trigger": self.trigger_number, "speed_cms": round(self._last_speed_cms, 2),
        })

    # ── input ────────────────────────────────────────────────────────────
    def _handle_command(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return  # the sketch ignores lines it cannot match
        cmd = msg.get("cmd")
        value = msg.get("value")
        with self._lock:
            self.received_commands.append(msg)
            if cmd == "get_config":
                self._emit_config()
            elif cmd == "reset_count":
                self.encoder_count = 0
                self._frac = 0.0
                self.last_trigger_count = 0
                self._last_speed_count = 0
                self._last_speed_ms = time.monotonic() * 1000
                self._last_speed_cms = 0.0
                self._emit_ack("reset_count", True)
            elif cmd == "set_trigger_enabled":
                self.trigger_enabled = bool(value) if not isinstance(value, str) else value.lower() == "true"
                self.last_trigger_count = self.encoder_count
                self._emit_ack(cmd, True)
                self._emit_config()
            elif cmd == "set_trigger_interval":
                self._set_numeric(cmd, "trigger_interval", value, int, lambda v: v > 0)
            elif cmd == "set_counts_per_cm":
                self._set_numeric(cmd, "counts_per_cm", value, float, lambda v: v > 0)
            elif cmd == "set_pulse_width_ms":
                self._set_numeric(cmd, "pulse_width_ms", value, int, lambda v: 0 < v <= 500)
            elif cmd == "set_speed_report_interval_ms":
                self._set_numeric(cmd, "speed_report_interval_ms", value, int, lambda v: 100 <= v <= 10000)
            elif cmd == "fire_trigger":
                self._emit_ack(cmd, True)
                self._fire_trigger("serial", self.encoder_count)

    def _set_numeric(self, cmd: str, attr: str, value, cast, valid) -> None:
        try:
            v = cast(value)
        except (TypeError, ValueError):
            v = None
        if v is not None and valid(v):
            setattr(self, attr, v)
            self._emit_ack(cmd, True)
            self._emit_config()
        else:
            self._emit_ack(cmd, False)

    # ── threads ──────────────────────────────────────────────────────────
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(0.2)
            with self._lock:
                self._client = conn
                self.connections += 1
            self._emit({"type": "startup", "msg": "Trigger controller started"})
            self._read_client(conn)

    def _read_client(self, conn: socket.socket) -> None:
        buf = b""
        while not self._stop.is_set():
            with self._lock:
                if self._client is not conn:
                    return
            try:
                chunk = conn.recv(512)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("ascii", errors="ignore").strip()
                if text:
                    self._handle_command(text)
        with self._lock:
            if self._client is conn:
                self._client = None

    def _physics_loop(self) -> None:
        last = time.monotonic()
        self._last_speed_ms = self._last_report_ms = last * 1000
        while not self._stop.is_set():
            time.sleep(_TICK_S)
            now = time.monotonic()
            dt, last = now - last, now
            with self._lock:
                # The belt keeps moving whether or not a host is connected, as on the real machine.
                self._frac += self.speed_cms * self.counts_per_cm * dt
                whole = int(self._frac)
                self._frac -= whole
                self.encoder_count += whole
                count = self.encoder_count
                if self.trigger_enabled:
                    if abs(count - self.last_trigger_count) >= self.trigger_interval:
                        self.last_trigger_count = count
                        self._fire_trigger("encoder", count)
                else:
                    self.last_trigger_count = count  # no burst on re-enable

                now_ms = now * 1000
                if now_ms - self._last_report_ms >= self.speed_report_interval_ms:
                    self._last_report_ms = now_ms
                    delta_ms = now_ms - self._last_speed_ms
                    if delta_ms > 0:
                        dist_cm = abs(count - self._last_speed_count) / self.counts_per_cm
                        self._last_speed_cms = dist_cm / (delta_ms / 1000.0)
                    self._last_speed_count = count
                    self._last_speed_ms = now_ms
                    self._emit({
                        "type": "speed", "speed_cms": round(self._last_speed_cms, 2),
                        "count": count, "trigger_enabled": self.trigger_enabled,
                    })


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=7000)
    ap.add_argument("--speed", type=float, default=20.0, help="belt speed in cm/s (default 20)")
    args = ap.parse_args()
    dev = FakeArduino(port=args.port, speed_cms=args.speed).start()
    print(f"fake Arduino listening on {dev.url}  (belt {args.speed} cm/s). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        dev.stop()


if __name__ == "__main__":
    main()
