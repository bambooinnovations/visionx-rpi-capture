"""The hardware mocks (mocks/): fake Arduino protocol, and the full no-hardware pipeline run."""
from __future__ import annotations

import json
import socket
import time

import pytest

from mocks.fake_arduino import FakeArduino


class Client:
    """A minimal serial-port stand-in for talking to FakeArduino."""

    def __init__(self, dev: FakeArduino) -> None:
        self.s = socket.create_connection(("127.0.0.1", dev.port), timeout=0.3)
        self.buf = b""

    def send(self, **cmd) -> None:
        self.s.sendall((json.dumps(cmd) + "\n").encode())

    def read(self, seconds: float = 0.6) -> list[dict]:
        end, out = time.time() + seconds, []
        while time.time() < end:
            try:
                self.buf += self.s.recv(4096)
            except socket.timeout:
                pass
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                out.append(json.loads(line))
        return out

    def close(self) -> None:
        self.s.close()


@pytest.fixture()
def dev():
    d = FakeArduino(speed_cms=0.0).start()
    yield d
    d.stop()


def test_startup_and_get_config(dev):
    c = Client(dev)
    msgs = c.read(0.3)
    assert msgs[0]["type"] == "startup"
    c.send(cmd="get_config")
    cfg = next(m for m in c.read(0.3) if m["type"] == "config")
    assert cfg["trigger_interval"] == 1180 and cfg["counts_per_cm"] == 118.0
    c.close()


def test_set_commands_ack_and_reject_bad_values(dev):
    c = Client(dev)
    c.read(0.2)
    c.send(cmd="set_trigger_interval", value=500)
    c.send(cmd="set_trigger_interval", value=-3)
    c.send(cmd="set_counts_per_cm", value=0)
    acks = [m for m in c.read(0.5) if m["type"] == "ack"]
    assert [a["ok"] for a in acks] == [True, False, False]
    assert dev.trigger_interval == 500 and dev.counts_per_cm == 118.0
    c.close()


def test_moving_belt_fires_distance_based_triggers(dev):
    dev.trigger_interval, dev.counts_per_cm = 100, 100.0   # one trigger per centimetre
    c = Client(dev)
    c.read(0.1)
    dev.set_speed(50.0)                                      # 50 cm/s -> ~50 triggers/s
    msgs = c.read(1.0)
    trig = [m for m in msgs if m["type"] == "trigger"]
    assert 30 <= len(trig) <= 60
    assert [t["trigger"] for t in trig] == list(range(1, len(trig) + 1))
    assert all(t["source"] == "encoder" for t in trig)
    gaps = [b["count"] - a["count"] for a, b in zip(trig, trig[1:])]
    assert all(g >= 100 for g in gaps)
    assert any(m["type"] == "speed" and 35 < m["speed_cms"] < 65 for m in msgs)
    c.close()


def test_reset_count_and_fire_trigger(dev):
    c = Client(dev)
    c.read(0.1)
    dev.set_speed(10.0)
    time.sleep(0.3)
    assert dev.encoder_count > 0
    dev.set_speed(0.0)
    c.send(cmd="reset_count")
    c.send(cmd="fire_trigger")
    msgs = c.read(0.4)
    assert dev.encoder_count == 0
    assert any(m["type"] == "ack" and m["cmd"] == "reset_count" for m in msgs)
    t = next(m for m in msgs if m["type"] == "trigger")
    assert t["source"] == "serial" and t["count"] == 0
    c.close()


def test_disabled_trigger_does_not_burst_on_reenable(dev):
    dev.trigger_interval, dev.counts_per_cm = 100, 100.0
    c = Client(dev)
    c.read(0.1)
    c.send(cmd="set_trigger_enabled", value=False)
    c.read(0.1)
    dev.set_speed(50.0)
    assert not [m for m in c.read(0.5) if m["type"] == "trigger"]
    dev.set_speed(0.0)
    c.send(cmd="set_trigger_enabled", value=True)
    assert not [m for m in c.read(0.4) if m["type"] == "trigger"]
    c.close()


def test_drop_connection_lets_a_client_reconnect(dev):
    c = Client(dev)
    c.read(0.2)
    assert dev.connected
    dev.drop_connection()
    c.close()
    c2 = Client(dev)
    assert c2.read(0.3)[0]["type"] == "startup" and dev.connections == 2
    c2.close()


def test_garbage_input_is_ignored(dev):
    c = Client(dev)
    c.read(0.2)
    c.s.sendall(b"\x00not json{{\r\n")
    c.send(cmd="get_config")
    assert any(m["type"] == "config" for m in c.read(0.4))
    c.close()


def test_full_pipeline_end_to_end():
    """Fake Arduino + fake cameras + fake upload server + the real app. About 20 s."""
    pytest.importorskip("piexif")
    from mocks import e2e

    res = e2e.run()
    failed = [f"{n}: {d}" for n, ok, d in res.rows if not ok]
    assert not failed, "\n".join(failed)


def test_serial_urls_are_refused_outside_mock_mode(monkeypatch):
    import config
    from camera.mindvision_trigger import is_serial_url, url_ports_allowed

    assert is_serial_url("socket://10.0.0.5:9") and not is_serial_url("/dev/ttyACM0") and not is_serial_url("COM3")
    monkeypatch.setattr(config, "CAMERA_TYPE", "mindvision")
    assert not url_ports_allowed()
    monkeypatch.setattr(config, "CAMERA_TYPE", "mock_mindvision")
    assert url_ports_allowed()
