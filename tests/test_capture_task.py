"""Backend-driven capture task: state machine, persistence, tagging, discard, HTTP and websocket.

The state machine is driven against a stub listener (no serial, no cameras); the listener's own task support
(tagging, counters, discard, exact pending counts) is tested on the real SerialTriggerListener with no cameras.
The full pipeline, with fake Arduino and cameras, is checked by `python -m mocks.e2e`.
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
import time
from pathlib import Path

import pytest

import capture_task
from capture_task import DRAINING, IDLE, INTERRUPTED, RUNNING, TaskError, TaskManager

pytest.importorskip("flask")


@pytest.fixture(autouse=True)
def _fabric_station(monkeypatch):
    monkeypatch.setattr(capture_task.config, "is_capture_station", lambda: False)


class StubListener:
    """Just the surface TaskManager uses."""

    def __init__(self) -> None:
        self.running = True
        self.serial_connected = True
        self.ack_ok = True
        self.pending_total = 0
        self.mem_pending = 0
        self.callbacks = []
        self.commands: list[dict] = []
        self.tag: str | None = None
        self.counters = {"triggers": 0, "captured": 0, "capture_failed": 0, "uploaded": 0, "upload_failed": 0, "discarded": 0}
        self.discards: list[str | None] = []
        self.trigger_enabled = True
        self.connect_count = 1
        self.kicks = 0
        self.gate_open = True

    def add_event_listener(self, cb): self.callbacks.append(cb)
    def set_trigger_state(self, enabled): self.trigger_enabled = enabled
    def send_command(self, cmd): self.commands.append(cmd)
    def kick_disk_retry(self): self.kicks += 1
    def begin_task(self, task_id): self.tag = task_id; self.counters = {k: 0 for k in self.counters}
    def end_task(self): self.tag = None
    def task_counters(self): return dict(self.counters)
    def task_pending(self, task_id): return {"capture": 0, "stitch": self.pending_total, "raw": 0, "disk_retry": 0, "total": self.pending_total}
    def memory_pending(self): return self.mem_pending
    def start(self, port, baud): self.running = True; self.starts = getattr(self, "starts", 0) + 1
    def stop(self): self.running = False; self.stops = getattr(self, "stops", 0) + 1
    def set_trigger_gate(self, open_): self.gate_open = open_

    def send_command_wait_ack(self, cmd, timeout=3.0):
        self.commands.append(cmd)
        return self.ack_ok

    def discard_task(self, task_id):
        self.discards.append(task_id)
        self.pending_total = 0
        return {"queued_captures": 1, "upload_jobs": 2, "aborted_jobs": 0, "disk_files": 3, "files": ["a.jpg"]}

    def status(self):
        return {"connect_count": self.connect_count, "trigger_enabled": self.trigger_enabled, "running": self.running,
                "speed_cms": 0.0, "distance_mm": 0.0, "encoder_count": 0, "serial_connected": True}

    def queue_depths(self):
        return {"camera_queues": {}, "camera_queue_maxsize": 0, "capture_inflight": 0, "collector_pending": 0,
                "stitch_pending": self.pending_total, "stitch_pending_mb": 0, "raw_pending": 0, "raw_pending_mb": 0,
                "disk_retry": 0, "disk_spill": 0, "pending_total": self.pending_total}

    def emit(self, event_type, **data):
        for cb in self.callbacks:
            cb({"type": event_type, "data": data})


def make(tmp_path, **kw):
    lst = StubListener()
    mgr = TaskManager(lst, {}, tmp_path / "task_state.json", watch_interval_s=0.02, ack_timeout_s=0.1,
                      serial_connect_timeout_s=0.3, discard_quiesce_s=0.3, **kw)
    return lst, mgr


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def disk_state(tmp_path) -> dict:
    return json.loads((tmp_path / "task_state.json").read_text())


# ── start ────────────────────────────────────────────────────────────────────

def test_start_resets_tags_and_enables_triggers(tmp_path):
    lst, mgr = make(tmp_path)
    out = mgr.start("job-1")
    assert out["state"] == RUNNING and out["task_id"] == "job-1"
    assert lst.tag == "job-1"
    cmds = [c["cmd"] + str(c.get("value", "")) for c in lst.commands]
    # disabled first, distance zero confirmed, only then enabled again
    assert cmds == ["set_trigger_enabledFalse", "reset_count", "set_trigger_enabledTrue"]
    assert disk_state(tmp_path)["state"] == RUNNING


def test_start_is_idempotent_for_same_id_and_refused_for_another(tmp_path):
    _, mgr = make(tmp_path)
    mgr.start("job-1")
    assert mgr.start("job-1")["state"] == RUNNING
    with pytest.raises(TaskError) as e:
        mgr.start("job-2")
    assert e.value.status == 409 and e.value.extra["active_task_id"] == "job-1"


@pytest.mark.parametrize("bad", [None, "", "has space", "x" * 129, "semi;colon", True, 1.5, ["a"]])
def test_invalid_task_ids_are_rejected(tmp_path, bad):
    _, mgr = make(tmp_path)
    with pytest.raises(TaskError) as e:
        mgr.start(bad)
    assert e.value.status == 400


def test_integer_task_id_is_accepted_as_string(tmp_path):
    _, mgr = make(tmp_path)
    assert mgr.start(42)["task_id"] == "42"


def test_unconfirmed_reset_fails_start_and_leaves_idle(tmp_path):
    lst, mgr = make(tmp_path)
    lst.ack_ok = False
    with pytest.raises(TaskError) as e:
        mgr.start("job-1")
    assert e.value.status == 503 and e.value.code == "reset_unconfirmed"
    assert mgr.state == IDLE and lst.tag is None
    assert not (tmp_path / "task_state.json").exists()


def _disk_state_or_none(tmp_path):
    try:
        return disk_state(tmp_path)["state"]
    except (OSError, ValueError):
        return None  # mid-rewrite


def _events(mgr):
    seen = []
    mgr.add_subscriber(seen.append)
    return seen


def test_reconnect_while_running_interrupts_the_task(tmp_path):
    lst, mgr = make(tmp_path)
    seen = _events(mgr)
    mgr.start("job-1")
    lst.commands.clear()
    lst.connect_count += 1                      # the serial link reopened: the Arduino's encoder is back at 0
    lst.emit("arduino.connected")
    assert mgr.state == INTERRUPTED and mgr.task_id == "job-1"
    assert lst.gate_open is False               # triggers are dropped on receipt until the backend closes the task
    assert lst.trigger_enabled is False
    assert {"cmd": "set_trigger_enabled", "value": False} in lst.commands
    assert disk_state(tmp_path)["state"] == INTERRUPTED
    ev = [e for e in seen if e["type"] == "task.interrupted"]
    assert ev and ev[0]["data"]["reason"] == "arduino_reconnected"
    assert mgr.status()["arduino_reconnects"] == 1


def test_reconnect_is_ignored_when_not_running(tmp_path):
    lst, mgr = make(tmp_path)
    lst.connect_count += 1
    lst.emit("arduino.connected")               # idle
    assert mgr.state == IDLE and lst.gate_open is True
    mgr.start("job-1")
    lst.pending_total = 2
    mgr.stop("job-1", wait_s=0)
    assert mgr.state == DRAINING
    lst.connect_count += 1
    lst.emit("arduino.connected")               # draining: no triggers anyway, the watcher keeps them off
    assert mgr.state == DRAINING


def test_interrupted_by_reconnect_can_be_stopped_and_reopens_the_gate(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.connect_count += 1
    lst.emit("arduino.connected")
    status, body = mgr.stop("job-1")
    assert status == 200 and body["state"] == IDLE
    assert lst.gate_open is True                # idle again: manual modes get their triggers back


def test_start_opens_and_stop_closes_the_trigger_gate(tmp_path):
    lst, mgr = make(tmp_path)
    lst.gate_open = False
    mgr.start("job-1")
    assert lst.gate_open is True
    lst.pending_total = 1
    mgr.stop("job-1", wait_s=0)
    assert lst.gate_open is False
    lst.pending_total = 0
    assert wait_for(lambda: mgr.state == IDLE)
    assert lst.gate_open is True


def test_recovered_interrupted_task_keeps_the_gate_closed(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst2, mgr2 = make(tmp_path)                 # the Pi restarted
    mgr2.recover()
    assert mgr2.state == INTERRUPTED and lst2.gate_open is False


# ── drain timeout ────────────────────────────────────────────────────────────

def test_drain_timeout_closes_the_task_without_discarding(tmp_path):
    lst, mgr = make(tmp_path, drain_timeout_s=0.2)
    seen = _events(mgr)
    mgr.start("job-1")
    lst.pending_total = 3                       # uploads that never finish (destination down)
    status, _ = mgr.stop("job-1", wait_s=0)
    assert status == 202 and mgr.state == DRAINING
    assert wait_for(lambda: mgr.state == IDLE)
    last = mgr.status()["last"]
    assert last["drain_timed_out"] is True and last["forced"] is False
    assert last["leftover"]["total"] == 3
    assert lst.discards == []                   # nothing was thrown away
    assert lst.tag is None and lst.gate_open is True
    assert wait_for(lambda: _disk_state_or_none(tmp_path) == IDLE)  # state flips before the file is rewritten
    names = [e["type"] for e in seen]
    assert "task.drain_timeout" in names and "task.finished" in names
    # the backend can still ask: same id on an idle manager returns the summary
    assert mgr.stop("job-1")[1]["summary"]["drain_timed_out"] is True


def test_normal_finish_is_not_marked_timed_out(tmp_path):
    lst, mgr = make(tmp_path, drain_timeout_s=0.2)
    mgr.start("job-1")
    status, body = mgr.stop("job-1")
    assert status == 200 and body["summary"]["drain_timed_out"] is False and "leftover" not in body["summary"]


def test_drain_timeout_zero_waits_forever(tmp_path):
    lst, mgr = make(tmp_path, drain_timeout_s=0)
    mgr.start("job-1")
    lst.pending_total = 1
    mgr.stop("job-1", wait_s=0)
    time.sleep(0.4)
    assert mgr.state == DRAINING


class FakeCam:
    def __init__(self, mode, fail_to_hw=False):
        self.mode = mode
        self.fail_to_hw = fail_to_hw
        self.calls = []

    def set_mode(self, mode):
        self.calls.append(mode)
        if self.fail_to_hw and mode.name == "HARDWARE_TRIGGER":
            raise RuntimeError("camera busy")
        self.mode = mode


def make_cams(tmp_path, cams, **kw):
    lst = StubListener()
    mgr = TaskManager(lst, cams, tmp_path / "task_state.json", watch_interval_s=0.02, ack_timeout_s=0.1,
                      serial_connect_timeout_s=0.3, discard_quiesce_s=0.3, **kw)
    return lst, mgr


def test_failed_camera_switch_reverts_the_cameras_it_switched(tmp_path):
    from camera.mindvision import CameraMode
    ok = FakeCam(CameraMode.CAPTURE)
    bad = FakeCam(CameraMode.CAPTURE, fail_to_hw=True)
    lst, mgr = make_cams(tmp_path, {0: ok, 1: bad})
    with pytest.raises(TaskError) as e:
        mgr.start("job-1")
    assert e.value.code == "camera_mode_failed"
    assert ok.mode == CameraMode.CAPTURE and ok.calls == [CameraMode.HARDWARE_TRIGGER, CameraMode.CAPTURE]
    assert mgr.state == IDLE


def test_unconfirmed_reset_reverts_cameras_and_stops_listener_it_started(tmp_path):
    from camera.mindvision import CameraMode
    cam = FakeCam(CameraMode.CAPTURE)
    lst, mgr = make_cams(tmp_path, {0: cam})
    lst.running = False
    lst.ack_ok = False
    with pytest.raises(TaskError) as e:
        mgr.start("job-1")
    assert e.value.code == "reset_unconfirmed"
    assert cam.mode == CameraMode.CAPTURE
    assert lst.running is False and lst.stops == 1


def test_failed_start_leaves_alone_what_it_did_not_change(tmp_path):
    from camera.mindvision import CameraMode
    cam = FakeCam(CameraMode.HARDWARE_TRIGGER)
    lst, mgr = make_cams(tmp_path, {0: cam})
    lst.ack_ok = False
    with pytest.raises(TaskError):
        mgr.start("job-1")
    assert cam.mode == CameraMode.HARDWARE_TRIGGER and cam.calls == []
    assert lst.running is True and getattr(lst, "stops", 0) == 0


def test_start_fails_when_arduino_not_connected(tmp_path):
    lst, mgr = make(tmp_path)
    lst.serial_connected = False
    with pytest.raises(TaskError) as e:
        mgr.start("job-1")
    assert e.value.status == 503 and mgr.state == IDLE


def test_start_refused_on_capture_station(tmp_path, monkeypatch):
    _, mgr = make(tmp_path)
    monkeypatch.setattr(capture_task.config, "is_capture_station", lambda: True)
    with pytest.raises(TaskError) as e:
        mgr.start("job-1")
    assert e.value.status == 409


# ── stop ─────────────────────────────────────────────────────────────────────

def test_stop_with_empty_queue_goes_idle_with_summary(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.counters.update(triggers=5, captured=10, uploaded=10)
    status, body = mgr.stop("job-1")
    assert status == 200 and body["state"] == IDLE
    assert body["summary"]["captured"] == 10 and body["summary"]["forced"] is False
    assert lst.tag is None
    saved = disk_state(tmp_path)
    assert saved["state"] == IDLE and saved["last"]["task_id"] == "job-1"


def test_stop_waits_for_the_arduino_to_confirm_triggers_are_off(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.commands.clear()
    mgr.stop("job-1")
    assert lst.commands == [{"cmd": "set_trigger_enabled", "value": False}]


def test_stop_with_pending_uploads_is_202_then_finishes_by_itself(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.pending_total = 3
    status, body = mgr.stop("job-1", wait_s=0.1)
    assert status == 202 and body["state"] == DRAINING and body["pending"]["total"] == 3
    assert mgr.state == DRAINING and disk_state(tmp_path)["state"] == DRAINING
    assert lst.trigger_enabled is False  # capture is already off

    lst.pending_total = 0  # uploads finish
    assert wait_for(lambda: mgr.state == IDLE)
    assert mgr.status()["last"]["task_id"] == "job-1"


def test_stop_again_while_draining_is_still_202(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.pending_total = 2
    mgr.stop("job-1", wait_s=0)
    status, body = mgr.stop("job-1", wait_s=0)
    assert status == 202 and body["state"] == DRAINING


def test_start_is_refused_while_draining(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.pending_total = 1
    mgr.stop("job-1", wait_s=0)
    with pytest.raises(TaskError) as e:
        mgr.start("job-2")
    assert e.value.status == 409 and e.value.code == DRAINING


def test_force_stop_discards_and_goes_idle(tmp_path):
    lst, mgr = make(tmp_path)
    events = []
    mgr.add_subscriber(events.append)
    mgr.start("job-1")
    lst.pending_total = 7
    status, body = mgr.stop("job-1", force=True)
    assert status == 200 and body["state"] == IDLE
    assert lst.discards == ["job-1"]
    assert body["discarded"]["disk_files"] == 3 and body["summary"]["forced"] is True
    assert mgr.state == IDLE
    names = [e["type"] for e in events]
    assert "task.discarded" in names and names[-1] == "task.finished"


def test_force_stop_of_a_draining_task(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.pending_total = 5
    mgr.stop("job-1", wait_s=0)
    status, body = mgr.stop("job-1", force=True)
    assert status == 200 and body["state"] == IDLE and lst.discards == ["job-1"]


def test_stop_for_the_wrong_task_is_refused(tmp_path):
    _, mgr = make(tmp_path)
    mgr.start("job-1")
    with pytest.raises(TaskError) as e:
        mgr.stop("job-2")
    assert e.value.status == 409 and e.value.code == "task_mismatch"
    assert mgr.state == RUNNING  # a stale backend cannot stop a newer task


def test_stop_when_idle_is_idempotent_only_for_the_last_task(tmp_path):
    _, mgr = make(tmp_path)
    mgr.start("job-1")
    mgr.stop("job-1")
    status, body = mgr.stop("job-1")
    assert status == 200 and body["state"] == IDLE
    with pytest.raises(TaskError) as e:
        mgr.stop("other")
    assert e.value.status == 409


def test_next_task_can_start_after_one_finishes(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    mgr.stop("job-1")
    assert mgr.start("job-2")["task_id"] == "job-2"
    assert lst.counters["triggers"] == 0  # fresh counters


# ── restart recovery ─────────────────────────────────────────────────────────

def test_running_task_becomes_interrupted_after_a_restart(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.counters.update(triggers=9)
    mgr._persist()  # what the watcher's checkpoint does

    lst2, mgr2 = make(tmp_path)  # "the Pi came back"
    events = []
    mgr2.add_subscriber(events.append)
    mgr2.recover()
    assert mgr2.state == INTERRUPTED and mgr2.task_id == "job-1"
    assert mgr2.status()["triggers"] == 9  # last checkpoint survives
    assert lst2.tag is None  # nothing new is stamped
    assert {"cmd": "set_trigger_enabled", "value": False} in lst2.commands  # and triggers are held off
    assert any(e["type"] == "task.interrupted" for e in events)
    assert disk_state(tmp_path)["state"] == INTERRUPTED


def test_interrupted_blocks_start_until_the_backend_stops_it(tmp_path):
    _, mgr = make(tmp_path)
    mgr.start("job-1")
    _, mgr2 = make(tmp_path)
    mgr2.recover()
    with pytest.raises(TaskError) as e:
        mgr2.start("job-2")
    assert e.value.status == 409 and e.value.code == INTERRUPTED
    status, body = mgr2.stop("job-1")
    assert status == 200 and body["state"] == IDLE
    assert mgr2.start("job-2")["state"] == RUNNING


def test_interrupted_task_can_be_force_closed(tmp_path):
    _, mgr = make(tmp_path)
    mgr.start("job-1")
    lst2, mgr2 = make(tmp_path)
    mgr2.recover()
    lst2.pending_total = 4
    status, body = mgr2.stop("job-1", force=True)
    assert status == 200 and body["state"] == IDLE and lst2.discards == ["job-1"]


def test_draining_task_resumes_draining_after_a_restart(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    lst.pending_total = 2
    mgr.stop("job-1", wait_s=0)

    lst2, mgr2 = make(tmp_path)
    lst2.pending_total = 2
    mgr2.recover()
    assert mgr2.state == DRAINING
    lst2.pending_total = 0
    assert wait_for(lambda: mgr2.state == IDLE)


def test_idle_state_file_recovers_to_idle_and_keeps_last_summary(tmp_path):
    _, mgr = make(tmp_path)
    mgr.start("job-1")
    mgr.stop("job-1")
    _, mgr2 = make(tmp_path)
    mgr2.recover()
    assert mgr2.state == IDLE and mgr2.status()["last"]["task_id"] == "job-1"


def test_corrupt_state_file_is_ignored(tmp_path):
    (tmp_path / "task_state.json").write_text("{not json")
    _, mgr = make(tmp_path)
    mgr.recover()
    assert mgr.state == IDLE


def test_interrupted_keeps_triggers_off_if_the_arduino_resets(tmp_path):
    _, mgr = make(tmp_path)
    mgr.start("job-1")
    lst2, mgr2 = make(tmp_path)
    mgr2.recover()
    lst2.commands.clear()
    lst2.trigger_enabled = True  # a reconnect resets the board, which comes back with triggers on
    assert wait_for(lambda: {"cmd": "set_trigger_enabled", "value": False} in lst2.commands, timeout=4.0)


# ── guard for manual controls ────────────────────────────────────────────────

def test_guard_blocks_manual_controls_while_a_task_is_active(tmp_path):
    lst, mgr = make(tmp_path)
    assert mgr.guard("reset") is None
    mgr.start("job-1")
    for action in ("reset", "stop", "calibration", "regular"):
        assert mgr.guard(action).status == 409
    assert mgr.guard("hw_trigger") is None  # already running: harmless
    lst.pending_total = 1
    mgr.stop("job-1", wait_s=0)
    assert mgr.guard("hw_trigger").status == 409  # must not re-enable triggers while draining


# ── snapshot ─────────────────────────────────────────────────────────────────

def test_snapshot_has_everything_the_monitor_shows(tmp_path):
    _, mgr = make(tmp_path)
    snap = mgr.snapshot()
    assert set(snap) == {"task", "motion", "arduino", "queues", "totals", "active_style"}
    assert snap["task"]["state"] == IDLE
    assert {"speed_cms", "distance_mm", "encoder_count"} <= set(snap["motion"])
    assert {"pending_total", "stitch", "raw", "disk_retry", "capture_inflight"} <= set(snap["queues"])
    json.dumps(snap)  # must be serialisable as is


def test_arduino_reconnects_during_a_task_are_counted(tmp_path):
    lst, mgr = make(tmp_path)
    mgr.start("job-1")
    assert mgr.status()["arduino_reconnects"] == 0
    lst.connect_count += 1
    assert mgr.status()["arduino_reconnects"] == 1


# ── listener: tagging, counters, exact pending, discard ──────────────────────

@pytest.fixture()
def listener(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")  # noqa: F841 — the module imports it lazily; skip cleanly if absent
    from camera import mindvision_trigger as mt

    monkeypatch.setattr(mt.config, "HW_TRIGGER_LOCAL_SAVE_DIR", tmp_path / "save")
    monkeypatch.setattr(mt.config, "CAPTURE_TMP_DIR", tmp_path / "tmp")
    lst = mt.SerialTriggerListener(cameras={0: object(), 1: object()})
    yield lst
    for pool in (lst._stitch_pool, lst._raw_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def trigger_line(n=1, count=100):
    return json.dumps({"type": "trigger", "source": "encoder", "count": count, "trigger": n, "speed_cms": 10.0})


def test_triggers_are_stamped_with_the_active_task_only(listener):
    listener._handle_line(trigger_line(1))
    assert "task_id" not in listener._camera_queues[0].get_nowait()

    listener.begin_task("job-1")
    listener._handle_line(trigger_line(2))
    ev = listener._camera_queues[0].get_nowait()
    assert ev["task_id"] == "job-1" and ev["_gen"] == 1
    listener._camera_queues[1].get_nowait()  # camera 1 saw the untagged trigger first
    assert listener._camera_queues[1].get_nowait()["task_id"] == "job-1"
    assert listener.task_counters()["triggers"] == 1

    listener.end_task()
    listener._handle_line(trigger_line(3))
    assert "task_id" not in listener._camera_queues[0].get_nowait()


def test_closed_trigger_gate_drops_triggers(listener):
    listener.begin_task("job-1")
    listener.set_trigger_gate(False)
    listener._handle_line(trigger_line(1))
    assert listener._camera_queues[0].empty() and listener.queue_depths()["capture_inflight"] == 0
    assert listener.task_counters()["triggers"] == 0
    assert listener.status()["triggers_gated"] == 1
    # speed/encoder still update, so the monitor keeps showing the live motion
    assert listener.status()["encoder_count"] == 100

    listener.set_trigger_gate(True)
    listener._handle_line(trigger_line(2, count=200))
    assert listener._camera_queues[0].get_nowait()["task_id"] == "job-1"


def test_task_id_reaches_exif_and_upload_fields(listener, monkeypatch):
    listener.begin_task("job-1")
    listener._handle_line(trigger_line(1))
    ev = listener._camera_queues[0].get_nowait()
    assert listener._exif_extra(ev)["task_id"] == "job-1"

    from camera import mindvision_trigger as mt

    seen = {}

    class FakeRequests:
        @staticmethod
        def post(url, files=None, data=None, headers=None, timeout=None):
            seen.update(data)

            class R:
                status_code = 200

                def raise_for_status(self): pass

            return R()

    monkeypatch.setitem(sys.modules, "requests", FakeRequests)
    assert mt._upload_image(b"x", ev, url="http://x/upload")
    assert seen["task_id"] == "job-1"


def test_sidecar_keeps_the_task_id_but_not_internal_fields(listener, tmp_path):
    from camera import mindvision_trigger as mt

    listener.begin_task("job-1")
    listener._handle_line(trigger_line(1))
    ev = listener._camera_queues[0].get_nowait()
    path = mt._save_image_locally(b"jpeg", ev, filename="a.jpg")
    side = json.loads(path.with_suffix(".jpg.json").read_text())
    assert side["task_id"] == "job-1" and "_gen" not in side and "_received_at" not in side


def test_capture_inflight_counts_queued_triggers(listener):
    listener._handle_line(trigger_line(1))
    assert listener.queue_depths()["capture_inflight"] == 2  # one per camera
    assert listener.task_pending(None)["capture"] == 2


def test_pending_counters_are_released_when_a_queued_job_is_cancelled(listener):
    listener._stitch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()
    listener._stitch_submit(gate.wait, 5, byte_count=100)   # occupies the only worker
    listener._stitch_submit(lambda: None, byte_count=50)    # queued behind it
    assert listener._stitch_pending == 2

    dropped = listener.discard_task(None)
    assert dropped["upload_jobs"] == 1 and dropped["aborted_jobs"] == 1
    assert listener._stitch_pending == 1 and listener._stitch_pending_bytes == 100  # the cancelled one is released
    gate.set()
    assert wait_for(lambda: listener._stitch_pending == 0 and listener._stitch_pending_bytes == 0)
    assert not listener._jobs


def test_pending_counters_survive_a_pool_shutdown_with_cancel(listener):
    listener._stitch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()
    listener._stitch_submit(gate.wait, 5)
    for _ in range(3):
        listener._stitch_submit(lambda: None)
    listener._stitch_pool.shutdown(wait=False, cancel_futures=True)  # what stop() does
    gate.set()
    assert wait_for(lambda: listener._stitch_pending == 0)  # previously stuck at 3


def test_discard_clears_queued_triggers_and_marks_the_generation(listener, tmp_path):
    listener.begin_task("job-1")
    listener._handle_line(trigger_line(1))
    listener._handle_line(trigger_line(2))
    ev = {"task_id": "job-1", "_gen": 1}
    assert not listener._discarded(ev)

    dropped = listener.discard_task("job-1")
    assert dropped["queued_captures"] == 4
    assert listener.queue_depths()["capture_inflight"] == 0
    assert listener._discarded(ev)
    assert not listener._discarded({"_gen": 0})                       # untagged manual captures are never dropped
    assert not listener._discarded({"task_id": "job-2", "_gen": 2})   # nor is a later task


def test_discard_deletes_only_this_tasks_disk_files_and_spill_files(listener, tmp_path):
    from camera import mindvision_trigger as mt

    mt._save_image_locally(b"a", {"task_id": "job-1", "trigger": 1}, filename="a.jpg")
    mt._save_image_locally(b"b", {"task_id": "job-2", "trigger": 2}, filename="b.jpg")
    mt._save_image_locally(b"c", {"trigger": 3}, filename="c.jpg")
    save = tmp_path / "save"
    assert listener.task_pending("job-1")["disk_retry"] == 1

    # a queued job holding a spilled capture
    spill = tmp_path / "tmp" / "hw_trigger" / "spill"
    spill.mkdir(parents=True)
    spilled = spill / "stitch_1_0.jpg"
    spilled.write_bytes(b"x")
    listener._stitch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()
    listener._stitch_submit(gate.wait, 5)
    listener._stitch_submit(lambda *_: None, {0: (spilled, "s", {})})

    listener.begin_task("job-1")
    dropped = listener.discard_task("job-1")
    gate.set()
    assert dropped["disk_files"] == 1 and dropped["files"] == ["a.jpg"]
    assert not (save / "a.jpg").exists() and not (save / "a.jpg.json").exists()
    assert (save / "b.jpg").exists() and (save / "c.jpg").exists()
    assert not spilled.exists()
    assert listener.task_counters()["discarded"] >= 2


def test_per_task_counters_ignore_other_tasks(listener):
    listener.begin_task("job-1")
    listener._task_bump({"task_id": "job-1"}, "uploaded")
    listener._task_bump({"task_id": "old"}, "uploaded")
    listener._task_bump({}, "uploaded")
    assert listener.task_counters()["uploaded"] == 1


def test_ack_wakes_a_waiter_and_zeroes_the_distance_mirror(listener):
    listener._arduino_state.update(encoder_count=5000, speed_cms=4.0)
    listener._running_stub = True
    listener._thread = threading.current_thread()  # makes `running` true without opening a port
    result = {}
    t = threading.Thread(target=lambda: result.update(ok=listener.send_command_wait_ack({"cmd": "reset_count"}, 2.0)))
    t.start()
    assert wait_for(lambda: "reset_count" in listener._ack_waiters)
    listener._handle_line(json.dumps({"type": "ack", "cmd": "reset_count", "ok": True}))
    t.join(3)
    assert result["ok"] is True and listener._arduino_state["encoder_count"] == 0


def test_ack_timeout_returns_false(listener):
    listener._thread = threading.current_thread()
    assert listener.send_command_wait_ack({"cmd": "reset_count"}, 0.1) is False


def test_upload_abort_stops_retries(monkeypatch):
    pytest.importorskip("numpy")
    from camera import mindvision_trigger as mt

    monkeypatch.setattr(mt, "_get_retry_attempts", lambda: 5)
    calls = []

    class FakeRequests:
        @staticmethod
        def post(*a, **k):
            calls.append(1)
            raise RuntimeError("down")

    monkeypatch.setitem(sys.modules, "requests", FakeRequests)
    monkeypatch.setattr(mt.time, "sleep", lambda s: None)
    assert mt._upload_image(b"x", {}, url="http://x", abort=lambda: len(calls) >= 2) is False
    assert len(calls) == 2


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path):
    from flask import Flask

    from blueprints.task import create_blueprint

    lst, mgr = make(tmp_path)
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(mgr))
    app.testing = True
    return app.test_client(), lst, mgr


def test_http_start_stop_round_trip(client):
    c, lst, mgr = client
    r = c.post("/api/task/start", json={"task_id": "job-1"})
    assert r.status_code == 200 and r.get_json()["state"] == "running"
    r = c.get("/api/task/status")
    assert r.get_json()["task"]["id"] == "job-1"
    r = c.post("/api/task/stop", json={"task_id": "job-1"})
    assert r.status_code == 200 and r.get_json()["state"] == "idle"


def test_http_errors_carry_a_code(client):
    c, _, _ = client
    assert c.post("/api/task/start", json={}).get_json()["code"] == "invalid_task_id"
    assert c.post("/api/task/stop", json={"task_id": "x"}).status_code == 409
    c.post("/api/task/start", json={"task_id": "job-1"})
    r = c.post("/api/task/start", json={"task_id": "job-2"})
    assert r.status_code == 409 and r.get_json()["code"] == "task_active"


def test_http_force_must_be_a_real_boolean(client):
    c, _, mgr = client
    c.post("/api/task/start", json={"task_id": "job-1"})
    r = c.post("/api/task/stop", json={"task_id": "job-1", "force": "false"})  # "false" is truthy in Python
    assert r.status_code == 400 and mgr.state == RUNNING


def test_http_stop_202_while_draining(client):
    c, lst, _ = client
    c.post("/api/task/start", json={"task_id": "job-1"})
    lst.pending_total = 2
    r = c.post("/api/task/stop", json={"task_id": "job-1", "wait_s": 0})
    assert r.status_code == 202 and r.get_json()["state"] == "draining"


# ── websocket ────────────────────────────────────────────────────────────────

@pytest.fixture()
def ws_server(tmp_path):
    pytest.importorskip("flask_sock")
    from flask import Flask
    from werkzeug.serving import make_server

    from blueprints import task as task_bp

    lst, mgr = make(tmp_path)
    app = Flask(__name__)
    hub = task_bp.StreamHub(mgr, max_clients=2)
    task_bp.register_websocket(app, hub, mgr)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"ws://127.0.0.1:{srv.server_port}/ws/task", lst, mgr, hub
    srv.shutdown()


def recv_json(ws, timeout=3.0):
    raw = ws.receive(timeout=timeout)
    return json.loads(raw) if raw else None


def test_websocket_sends_state_on_connect_and_periodically(ws_server):
    from simple_websocket import Client

    url, _, _, _ = ws_server
    ws = Client.connect(url)
    try:
        first = recv_json(ws)
        assert first["v"] == 1 and first["type"] == "state" and first["seq"] == 1
        assert first["data"]["task"]["state"] == "idle" and isinstance(first["ts"], int)
        second = recv_json(ws, 4.0)
        assert second["type"] == "state" and second["seq"] == 2
    finally:
        ws.close()


def test_websocket_forwards_task_and_pipeline_events(ws_server):
    from simple_websocket import Client

    url, lst, mgr, _ = ws_server
    ws = Client.connect(url)
    try:
        recv_json(ws)
        mgr.start("job-1")
        lst.emit("upload.failed", filename="a.jpg")
        seen = {}
        end = time.time() + 4
        while time.time() < end and not {"task.started", "upload.failed"} <= set(seen):
            msg = recv_json(ws, 1.0)
            if msg and msg["type"] == "event":
                seen[msg["data"]["name"]] = msg["data"]
        assert seen["task.started"]["task_id"] == "job-1"
        assert seen["upload.failed"]["filename"] == "a.jpg"
    finally:
        ws.close()


def test_websocket_caps_clients_and_frees_the_slot_on_disconnect(ws_server):
    from simple_websocket import Client, ConnectionClosed

    url, _, _, hub = ws_server
    a, b = Client.connect(url), Client.connect(url)
    try:
        recv_json(a), recv_json(b)
        assert hub.client_count == 2
        c = Client.connect(url)
        with pytest.raises(ConnectionClosed):  # told to try later
            for _ in range(5):
                c.receive(timeout=2)
        a.close()
        assert wait_for(lambda: hub.client_count == 1, 5.0)
        d = Client.connect(url)
        assert recv_json(d)["type"] == "state"
        d.close()
    finally:
        b.close()


def test_slow_client_drops_oldest_instead_of_blocking_producers():
    from blueprints.task import CLIENT_QUEUE_SIZE, _Client

    c = _Client()
    for i in range(CLIENT_QUEUE_SIZE + 50):
        c.push(("event", {"i": i}, 0))  # must never block or raise
    assert c.q.qsize() == CLIENT_QUEUE_SIZE
    assert c.q.get_nowait()[1]["i"] == 50
