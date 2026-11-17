"""End-to-end check of the hardware-trigger pipeline with no hardware.

    python -m mocks.e2e            # run everything, exit 1 on any failure

Starts, all local and isolated in a temp dir (your configuration.toml, data/ and runtime_config.json are not touched):
  * mocks.fake_arduino      - the encoder board, over TCP (socket://)
  * mocks.fake_upload_server - the platform endpoint, with failure injection
  * the real app (app.py) with `[camera] type = "mock_mindvision"` (two fake MindVision cameras)

and then drives a simulated roll through the real decoder routes, checking what actually comes out the other end:
photos per trigger per camera, the distance written into each photo's EXIF, reset of the roll distance, recovery from
a serial drop, retry on upload failure and the local-save fallback.

What this proves is the software. It cannot prove the sketch, wiring, encoder counts or camera trigger line.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import piexif
import requests

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from camera.mindvision_trigger import compute_arduino_params  # noqa: E402
from mocks.fake_arduino import FakeArduino  # noqa: E402
from mocks.fake_upload_server import FakeUploadServer  # noqa: E402

WHEEL_MM, PPR, INTERVAL_MM = 300.0, 600, 400.0
BELT_CMS = 80.0
CAMERAS = 2
DRAIN_TIMEOUT_S = 12  # [task] drain_timeout_s for the mock app; long enough for a healthy drain, short enough to test


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(pred, timeout: float, what: str, poll: float = 0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(poll)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for: {what}")


def exif_comment(jpeg: bytes) -> dict:
    """The JSON the app writes into EXIF UserComment (exposure, gain, ... and the trigger distance)."""
    raw = piexif.load(jpeg)["Exif"][piexif.ExifIFD.UserComment]
    return json.loads(raw[8:].decode("ascii"))


class Harness:
    """The fake hardware plus the real app, wired together. Use as a context manager."""

    def __init__(self, keep_logs: bool = False) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="visionx_mock_")
        self.dir = Path(self._tmp.name)
        self.keep_logs = keep_logs
        self.arduino = FakeArduino(speed_cms=0.0)
        self.upload = FakeUploadServer()
        self.app_port = _free_port()
        self.base = f"http://127.0.0.1:{self.app_port}"
        self.save_dir = self.dir / "hw_captures"
        self.log_path = self.dir / "app.log"
        self._proc: subprocess.Popen | None = None
        derived = compute_arduino_params(WHEEL_MM, PPR, INTERVAL_MM)
        self.counts_per_cm = derived["counts_per_cm"]
        self.trigger_interval = derived["trigger_interval"]

    # -- setup / teardown --------------------------------------------------
    def _write_config(self) -> Path:
        d = self.dir.as_posix()
        cfg = self.dir / "configuration.toml"
        cfg.write_text(f"""
[server]
env = "dev"
[station]
type = "fabric"
[camera]
type = "mock_mindvision"
mock_count = {CAMERAS}
[capture]
tmp_dir = "{d}/captures"
[metrics]
db_path = "{d}/metrics.db"
[profiles]
db_path = "{d}/profiles.db"
[hw_trigger]
auto_start = false
serial_port = "{self.arduino.url}"
destination_url = "{self.upload.url}"
retry_attempts = 3
timeout_seconds = 5
save_local = true
local_save_dir = "{self.save_dir.as_posix()}"
[task]
drain_timeout_s = {DRAIN_TIMEOUT_S}
""", encoding="utf-8")
        return cfg

    def __enter__(self) -> "Harness":
        self.arduino.start()
        self.upload.start()
        env = {
            **os.environ,
            "VISIONX_CONFIG": str(self._write_config()),
            "VISIONX_RUNTIME_CONFIG": str(self.dir / "runtime_config.json"),
            "PYTHONPATH": str(REPO),
            "PYTHONUNBUFFERED": "1",
        }
        code = f"import app; app.app.run(host='127.0.0.1', port={self.app_port}, threaded=True)"
        self._log = open(self.log_path, "wb")
        self._proc = subprocess.Popen([sys.executable, "-c", code], cwd=self.dir, env=env,
                                      stdout=self._log, stderr=subprocess.STDOUT)
        try:
            wait_for(self._app_up, 40, "app to start")
        except TimeoutError:
            self.__exit__(None, None, None)
            raise
        return self

    def _app_up(self) -> bool:
        if self._proc.poll() is not None:
            raise RuntimeError(f"app exited early (code {self._proc.returncode}):\n{self.log_tail()}")
        try:
            return requests.get(f"{self.base}/api/decoder/status", timeout=2).ok
        except requests.RequestException:
            return False

    def __exit__(self, *exc) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._log.close()
        self.arduino.stop()
        self.upload.stop()
        if not self.keep_logs:
            self._tmp.cleanup()

    def log_tail(self, n: int = 40) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return "(no log)"

    # -- helpers -----------------------------------------------------------
    def api(self, method: str, path: str, **kw):
        r = requests.request(method, self.base + path, timeout=10, **kw)
        return r

    def status(self) -> dict:
        return self.api("GET", "/api/decoder/status").json()

    def groups(self) -> dict[int, dict]:
        """Uploads grouped by trigger number: {trigger: {camera_id: Upload}} (the final distance is on each)."""
        out: dict[int, dict] = {}
        for u in self.upload.snapshot():
            if "trigger_number" not in u.fields or not u.fields.get("camera_id"):
                continue
            out.setdefault(int(u.fields["trigger_number"]), {})[int(u.fields["camera_id"])] = u
        return out

    def complete_triggers(self) -> list[int]:
        return sorted(t for t, cams in self.groups().items() if len(cams) == CAMERAS)

    def distance_mm(self, trigger: int) -> float:
        up = next(iter(self.groups()[trigger].values()))
        return exif_comment(up.image)["distance_mm"]


# ── scenarios ─────────────────────────────────────────────────────────────
class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)


def scenario_start_and_configure(h: Harness, r: Results) -> None:
    print("start decoder and configure wheel/encoder/interval")
    resp = h.api("POST", "/api/decoder/start", json={"port": h.arduino.url})
    r.check("decoder starts on the fake serial port", resp.ok, resp.text[:120])
    wait_for(lambda: h.status()["serial_connected"], 15, "serial_connected")
    r.check("listener connected to the fake Arduino", h.arduino.connected)

    resp = h.api("PATCH", "/api/decoder/config",
                 json={"wheel_diameter_mm": WHEEL_MM, "encoder_ppr": PPR, "capture_interval_mm": INTERVAL_MM})
    r.check("config PATCH accepted", resp.ok, resp.text[:120])
    wait_for(lambda: h.arduino.trigger_interval == h.trigger_interval, 5, "interval pushed to Arduino")
    r.check("derived trigger_interval reached the Arduino",
            h.arduino.trigger_interval == h.trigger_interval, f"{h.arduino.trigger_interval} counts")
    r.check("derived counts_per_cm reached the Arduino", abs(h.arduino.counts_per_cm - h.counts_per_cm) < 1e-3)
    wait_for(lambda: h.status().get("arduino_config", {}).get("counts_per_cm"), 5, "config echoed back")


def scenario_roll(h: Harness, r: Results) -> None:
    print(f"simulated roll at {BELT_CMS:.0f} cm/s, a photo every {INTERVAL_MM:.0f} mm")
    h.arduino.set_speed(BELT_CMS)
    try:
        wait_for(lambda: len(h.complete_triggers()) >= 8, 30, "8 complete triggers")
    except TimeoutError as e:
        r.check("photos arrive from both cameras", False, str(e))
        return
    trig = h.complete_triggers()
    r.check("every trigger produced one photo per camera",
            all(set(h.groups()[t]) == set(range(CAMERAS)) for t in trig), f"{len(trig)} triggers")
    r.check("trigger numbers are consecutive", trig == list(range(trig[0], trig[0] + len(trig))), str(trig[:10]))

    # EXIF distance on the photos
    cpc = round(h.counts_per_cm, 2)  # the Arduino reports counts_per_cm to 2 decimals, the app uses that
    errors = []
    for t in trig:
        cams = h.groups()[t]
        dists = {c: exif_comment(u.image).get("distance_mm") for c, u in cams.items()}
        if None in dists.values():
            errors.append(f"trigger {t}: no distance_mm in EXIF")
            continue
        if len(set(dists.values())) != 1:
            errors.append(f"trigger {t}: cameras disagree {dists}")
        want = round(int(next(iter(cams.values())).fields["trigger_count"]) / cpc * 10.0, 1)
        if abs(next(iter(dists.values())) - want) > 0.11:
            errors.append(f"trigger {t}: EXIF {dists} vs count-derived {want}")
    r.check("every photo's EXIF carries distance_mm, matching the encoder count", not errors, "; ".join(errors[:3]))

    ds = [h.distance_mm(t) for t in trig]
    gaps = [b - a for a, b in zip(ds, ds[1:])]
    # The board fires on the first loop pass after the count crosses the interval, so a trigger can overshoot a little.
    slack = BELT_CMS * 10.0 * 0.02  # mm travelled in ~20 ms
    r.check(f"photos are spaced ~{INTERVAL_MM:.0f} mm apart",
            all(INTERVAL_MM - 0.5 <= g <= INTERVAL_MM + slack for g in gaps),
            f"gaps min {min(gaps):.1f} max {max(gaps):.1f} mm")
    comment = exif_comment(next(iter(h.groups()[trig[0]].values())).image)
    r.check("EXIF also records trigger_number and encoder_count",
            comment.get("trigger_number") == trig[0] and "encoder_count" in comment, str(comment))

    st = h.status()
    r.check("status reports a live belt speed", abs(st["speed_cms"] - BELT_CMS) < BELT_CMS * 0.15, f"{st['speed_cms']} cm/s")
    r.check("status reports distance_mm", isinstance(st.get("distance_mm"), (int, float)) and st["distance_mm"] > 0)
    page = h.api("GET", "/monitor")
    r.check("monitor page is served with the new-roll button", page.ok and "Start new roll" in page.text)


def scenario_new_roll(h: Harness, r: Results) -> None:
    print("start a new roll (reset distance)")
    before = max(h.complete_triggers())
    before_dist = h.distance_mm(before)
    resp = h.api("POST", "/api/decoder/reset-count")
    r.check("reset-count accepted", resp.ok, resp.text[:80])
    try:
        # a trigger already in flight at the reset can still carry the old distance, so look for the first new one
        t = wait_for(lambda: next((t for t in h.complete_triggers()
                                   if t > before and h.distance_mm(t) < INTERVAL_MM + 50), None), 20, "post-reset photo")
    except TimeoutError as e:
        r.check("distance restarts from 0 after a new roll", False, str(e))
        return
    r.check("distance restarts from 0 after a new roll", True,
            f"{before_dist:.0f} mm before, {h.distance_mm(t):.0f} mm on the first photo after")
    nxt = wait_for(lambda: next((x for x in h.complete_triggers() if x > t), None), 10, "next photo")
    gap = h.distance_mm(nxt) - h.distance_mm(t)
    r.check("spacing is unchanged after the reset", INTERVAL_MM - 0.5 <= gap <= INTERVAL_MM + 10, f"{gap:.1f} mm")


def scenario_serial_drop(h: Harness, r: Results) -> None:
    print("serial link drops, then recovers")
    conns = h.arduino.connections
    last = max(h.complete_triggers())
    h.arduino.drop_connection()
    try:
        wait_for(lambda: not h.status()["serial_connected"], 8, "serial_connected -> false", poll=0.05)
        st = h.status()
        r.check("status notices the dropped link and zeroes the speed", st["speed_cms"] == 0.0, f"speed {st['speed_cms']}")
    except TimeoutError as e:
        r.check("status notices the dropped link", False, str(e))
    try:
        wait_for(lambda: h.status()["serial_connected"] and h.arduino.connections > conns, 20, "reconnect")
        r.check("listener reconnects by itself", True)
        wait_for(lambda: any(t > last for t in h.complete_triggers()), 20, "photos resume")
        r.check("photos resume after reconnect", True)
    except TimeoutError as e:
        r.check("listener reconnects and photos resume", False, str(e))

    # robustness: a garbage line from the board must not stop anything
    n = len(h.complete_triggers())
    h.arduino.send_garbage()
    try:
        wait_for(lambda: len(h.complete_triggers()) > n + 1, 15, "photos after garbage line")
        r.check("garbage on the serial line is ignored", True)
    except TimeoutError as e:
        r.check("garbage on the serial line is ignored", False, str(e))


def scenario_upload_failures(h: Harness, r: Results) -> None:
    print("upload failures: retry, then local-save fallback")
    st0 = h.status()
    n0, attempts0 = len(h.upload.snapshot()), h.upload.attempts
    h.upload.fail_next = 2  # fewer than retry_attempts: should be absorbed by retries
    try:
        wait_for(lambda: h.upload.fail_next == 0 and len(h.upload.snapshot()) > n0 + 1, 20, "retries absorb 2 failures")
        extra = (h.upload.attempts - attempts0) - (len(h.upload.snapshot()) - n0)
        r.check("transient upload failures are retried", extra >= 2, f"{extra} failed attempts")
        r.check("no upload counted as failed after retries", h.status()["uploads_failed"] == st0["uploads_failed"])
    except TimeoutError as e:
        r.check("transient upload failures are retried", False, str(e))

    h.upload.fail_next = 10_000  # the destination is down
    try:
        wait_for(lambda: h.status()["uploads_failed"] > st0["uploads_failed"], 30, "uploads_failed to rise")
        files = list(h.save_dir.glob("*.jpg")) if h.save_dir.exists() else []
        r.check("failed uploads fall back to the local save dir", bool(files), f"{len(files)} file(s)")
        if files:
            side = files[0].with_suffix(".jpg.json")
            r.check("local fallback keeps a metadata sidecar", side.exists())
            r.check("saved photo still has the EXIF distance", "distance_mm" in exif_comment(files[0].read_bytes()))
    except TimeoutError as e:
        r.check("failed uploads fall back to the local save dir", False, str(e))
    finally:
        h.upload.fail_next = 0


class WsCollector:
    """Connects to /ws/task and keeps every message it receives."""

    def __init__(self, base: str) -> None:
        from simple_websocket import Client

        self.msgs: list[dict] = []
        self._stop = False
        self.ws = Client.connect(base.replace("http://", "ws://") + "/ws/task")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            try:
                raw = self.ws.receive(timeout=0.5)
            except Exception:
                return
            if raw:
                self.msgs.append(json.loads(raw))

    def events(self, name: str) -> list[dict]:
        return [m["data"] for m in list(self.msgs) if m["type"] == "event" and m["data"].get("name") == name]

    def states(self) -> list[dict]:
        return [m["data"] for m in list(self.msgs) if m["type"] == "state"]

    def close(self) -> None:
        self._stop = True
        try:
            self.ws.close()
        except Exception:
            pass


def _tagged(h: Harness, task_id: str) -> list:
    return [u for u in h.upload.snapshot() if u.fields.get("task_id") == task_id]


def _disk_files_for(h: Harness, task_id: str) -> list[Path]:
    found = []
    for side in (h.save_dir.glob("*.jpg.json") if h.save_dir.exists() else []):
        try:
            if json.loads(side.read_text()).get("task_id") == task_id:
                found.append(side)
        except Exception:
            pass
    return found


def scenario_task(h: Harness, r: Results) -> None:
    print("backend-driven task: start, tag, graceful stop, drain, force stop")
    h.upload.fail_next = 0
    h.arduino.set_speed(BELT_CMS)
    ws = WsCollector(h.base)
    try:
        wait_for(lambda: ws.states(), 5, "first websocket state")
        r.check("websocket sends a full state on connect", "queues" in ws.states()[0] and "motion" in ws.states()[0])

        # -- start --------------------------------------------------------
        before = h.status()["encoder_count"]
        resp = h.api("POST", "/api/task/start", json={"task_id": "job-1"})
        r.check("task starts", resp.status_code == 200 and resp.json()["state"] == "running", resp.text[:120])
        r.check("starting the same task again is a no-op", h.api("POST", "/api/task/start", json={"task_id": "job-1"}).status_code == 200)
        other = h.api("POST", "/api/task/start", json={"task_id": "job-other"})
        r.check("a different task is refused while one runs", other.status_code == 409 and other.json()["active_task_id"] == "job-1")
        r.check("manual reset-count is refused during a task", h.api("POST", "/api/decoder/reset-count").status_code == 409)
        r.check("manual decoder stop is refused during a task", h.api("POST", "/api/decoder/stop").status_code == 409)
        r.check("invalid task id is rejected", h.api("POST", "/api/task/start", json={"task_id": "bad id!"}).status_code in (400, 409))

        wait_for(lambda: len({int(u.fields["trigger_number"]) for u in _tagged(h, "job-1") if u.fields.get("trigger_number")}) >= 6,
                 40, "6 tagged triggers")
        tagged = _tagged(h, "job-1")
        r.check("uploads carry the task id", all(u.fields["task_id"] == "job-1" for u in tagged), f"{len(tagged)} uploads")
        ids = {exif_comment(u.image).get("task_id") for u in tagged}
        r.check("EXIF carries the task id", ids == {"job-1"}, str(ids))
        first = min(tagged, key=lambda u: int(u.fields["trigger_number"]))
        d0 = exif_comment(first.image)["distance_mm"]
        r.check("distance restarted from zero at start", d0 < INTERVAL_MM * 1.3, f"first tagged photo at {d0} mm (encoder was {before} before)")

        # -- live telemetry -------------------------------------------------
        wait_for(lambda: any(s["task"]["state"] == "running" and s["task"]["triggers"] > 0 for s in ws.states()), 5, "running state with triggers")
        last = ws.states()[-1]
        r.check("state message mirrors the monitor page",
                {"task", "motion", "arduino", "queues", "totals"} <= set(last) and last["motion"]["speed_cms"] is not None)
        r.check("task started event was published", bool(ws.events("task.started")))
        seqs = [m["seq"] for m in ws.msgs]
        r.check("websocket sequence numbers increase", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))
        r.check("GET /api/task/status matches the socket shape", set(h.api("GET", "/api/task/status").json()) == set(last))

        # -- graceful stop with a healthy destination ------------------------
        resp = h.api("POST", "/api/task/stop", json={"task_id": "job-1"})
        body = resp.json()
        r.check("stop returns idle (200) when uploads already drained", resp.status_code == 200 and body["state"] == "idle", resp.text[:160])
        r.check("stop reports a summary with counters", body.get("summary", {}).get("captured", 0) >= 6, str(body.get("summary")))
        r.check("stop is idempotent", h.api("POST", "/api/task/stop", json={"task_id": "job-1"}).status_code == 200)
        n_after = len(h.upload.snapshot())
        time.sleep(2.0)
        r.check("no photos are taken after stop", len(h.upload.snapshot()) == n_after and not h.arduino.trigger_enabled)
        r.check("task finished event was published", bool(ws.events("task.finished")))
        r.check("stopping the wrong task id is refused", h.api("POST", "/api/task/stop", json={"task_id": "nope"}).status_code == 409)

        # -- stop while uploads are failing: draining, then finished ------------
        h.upload.fail_next = 10_000
        resp = h.api("POST", "/api/task/start", json={"task_id": "job-2"})
        r.check("second task starts", resp.status_code == 200, resp.text[:100])
        wait_for(lambda: h.api("GET", "/api/task/status").json()["task"]["triggers"] >= 4, 30, "4 triggers on job-2")
        resp = h.api("POST", "/api/task/stop", json={"task_id": "job-2", "wait_s": 0.5})
        r.check("stop answers 202 draining while uploads are pending", resp.status_code == 202 and resp.json()["state"] == "draining", resp.text[:160])
        r.check("a new task is refused while draining", h.api("POST", "/api/task/start", json={"task_id": "job-3"}).status_code == 409)
        r.check("capture stays off while draining", not h.arduino.trigger_enabled)
        h.upload.fail_next = 0  # the destination recovers
        try:
            wait_for(lambda: len(ws.events("task.finished")) >= 2, 60, "job-2 to drain")
            st = h.api("GET", "/api/task/status").json()["task"]
            r.check("draining finishes by itself once uploads succeed", st["state"] == "idle" and st["last"]["task_id"] == "job-2")
            r.check("nothing left on disk for the drained task", not _disk_files_for(h, "job-2"))
        except TimeoutError as e:
            r.check("draining finishes by itself once uploads succeed", False, f"{e}; {h.api('GET', '/api/task/status').json()['task']}")

        # -- force stop -----------------------------------------------------
        h.upload.fail_next = 10_000
        h.api("POST", "/api/task/start", json={"task_id": "job-3"})
        wait_for(lambda: h.api("GET", "/api/task/status").json()["task"]["triggers"] >= 4, 30, "4 triggers on job-3")
        resp = h.api("POST", "/api/task/stop", json={"task_id": "job-3", "force": True})
        body = resp.json()
        dropped = body.get("discarded", {})
        r.check("force stop goes idle immediately", resp.status_code == 200 and body.get("state") == "idle", resp.text[:200])
        r.check("force stop reports what it dropped", sum(dropped.get(k, 0) for k in ("upload_jobs", "aborted_jobs", "queued_captures", "disk_files")) > 0, str(dropped))
        r.check("no disk-backed files remain for a discarded task", not _disk_files_for(h, "job-3"))
        r.check("discard was announced", bool(ws.events("task.discarded")))
        count = len(_tagged(h, "job-3"))
        h.upload.fail_next = 0
        time.sleep(4.0)
        r.check("discarded photos are never uploaded afterwards", len(_tagged(h, "job-3")) == count and not _disk_files_for(h, "job-3"))
        st = h.api("GET", "/api/task/status").json()
        r.check("pipeline is empty after a force stop", st["queues"]["capture_inflight"] == 0 and st["queues"]["stitch"] == 0, str(st["queues"]))
        r.check("manual controls work again once idle", h.api("POST", "/api/decoder/reset-count").status_code == 200)
    finally:
        ws.close()
        h.upload.fail_next = 0


def scenario_task_reconnect_and_timeout(h: Harness, r: Results) -> None:
    print("task: serial reconnect interrupts it; drain timeout closes it without losing uploads")
    h.upload.fail_next = 0
    h.arduino.set_speed(BELT_CMS)
    ws = WsCollector(h.base)
    try:
        # -- a serial reconnect mid-run: the Arduino's encoder restarts, so the task must not carry on ------------
        h.api("POST", "/api/task/start", json={"task_id": "job-r"})
        wait_for(lambda: h.api("GET", "/api/task/status").json()["task"]["triggers"] >= 3, 30, "3 triggers on job-r")
        h.arduino.drop_connection()
        try:
            wait_for(lambda: h.api("GET", "/api/task/status").json()["task"]["state"] == "interrupted", 20, "interrupted")
            st = h.api("GET", "/api/task/status").json()["task"]
            r.check("a serial reconnect interrupts the running task", st["id"] == "job-r" and st["arduino_reconnects"] >= 1, str(st))
        except TimeoutError as e:
            r.check("a serial reconnect interrupts the running task", False, str(e))
        ev = ws.events("task.interrupted")
        r.check("interruption was announced with its reason", bool(ev) and ev[-1].get("reason") == "arduino_reconnected", str(ev[-1:]))
        wait_for(lambda: not h.arduino.trigger_enabled, 10, "triggers off")
        time.sleep(1.0)
        n = len(_tagged(h, "job-r"))
        time.sleep(3.0)
        r.check("no new photos are taken once interrupted", len(_tagged(h, "job-r")) == n and not h.arduino.trigger_enabled)
        r.check("a new task is refused while interrupted", h.api("POST", "/api/task/start", json={"task_id": "job-x"}).status_code == 409)
        resp = h.api("POST", "/api/task/stop", json={"task_id": "job-r"})
        r.check("the backend can close an interrupted task", resp.status_code == 200 and resp.json()["state"] == "idle", resp.text[:160])
        r.check("manual controls work again after closing it", h.api("POST", "/api/decoder/reset-count").status_code == 200)
        r.check("captured photos kept their task id", len(_tagged(h, "job-r")) >= 3)

        # -- drain timeout: destination stays down, the task closes anyway, nothing is lost ----------------------
        h.upload.fail_next = 10_000
        h.api("POST", "/api/task/start", json={"task_id": "job-t"})
        wait_for(lambda: h.api("GET", "/api/task/status").json()["task"]["triggers"] >= 3, 30, "3 triggers on job-t")
        resp = h.api("POST", "/api/task/stop", json={"task_id": "job-t", "wait_s": 0.5})
        r.check("stop answers draining while the destination is down", resp.status_code == 202, resp.text[:160])
        try:
            wait_for(lambda: h.api("GET", "/api/task/status").json()["task"]["state"] == "idle", DRAIN_TIMEOUT_S + 20, "drain timeout")
            last = h.api("GET", "/api/task/status").json()["task"]["last"]
            r.check("drain timeout closes the task", last["task_id"] == "job-t" and last["drain_timed_out"] is True and last["forced"] is False, str(last))
            r.check("the timeout reports what is left", last["leftover"]["total"] > 0, str(last.get("leftover")))
        except TimeoutError as e:
            r.check("drain timeout closes the task", False, str(e))
        try:
            wait_for(lambda: bool(ws.events("task.drain_timeout")), 5, "timeout event")
        except TimeoutError:
            pass
        r.check("timeout was announced", bool(ws.events("task.drain_timeout")))
        left = _disk_files_for(h, "job-t")
        r.check("failed uploads stay on disk after the timeout", len(left) > 0, f"{len(left)} files")
        h.upload.fail_next = 0  # the destination recovers
        try:
            wait_for(lambda: not _disk_files_for(h, "job-t"), 60, "retry of job-t files")
            r.check("they keep retrying and upload under the original task id", len(_tagged(h, "job-t")) > 0 and not _disk_files_for(h, "job-t"),
                    f"{len(_tagged(h, 'job-t'))} uploaded")
        except TimeoutError as e:
            r.check("they keep retrying and upload under the original task id", False, str(e))
    finally:
        ws.close()
        h.upload.fail_next = 0


def scenario_wrap_up(h: Harness, r: Results) -> None:
    print("wrap up")
    h.arduino.set_speed(0.0)
    st = h.status()
    r.check("no capture failed during the run", st["captures_failed"] == 0, f"{st['captures_ok']} captures ok")
    resp = h.api("POST", "/api/decoder/stop")
    r.check("decoder stops cleanly", resp.ok, resp.text[:80])


SCENARIOS = [scenario_start_and_configure, scenario_roll, scenario_new_roll, scenario_serial_drop,
             scenario_upload_failures, scenario_task, scenario_task_reconnect_and_timeout, scenario_wrap_up]


def run(keep_logs: bool = False) -> Results:
    r = Results()
    with Harness(keep_logs=keep_logs) as h:
        for scenario in SCENARIOS:
            try:
                scenario(h, r)
            except Exception as exc:  # a crashed scenario is a failure, but later ones may still tell us something
                r.check(f"{scenario.__name__} ran without error", False, f"{type(exc).__name__}: {exc}")
        if not r.ok:
            print("\n--- app log (tail) ---\n" + h.log_tail(60))
            if keep_logs:
                print(f"\nlogs kept in {h.dir}")
    return r


def main() -> None:
    ap = argparse.ArgumentParser(description="End-to-end hardware-trigger pipeline check with mocked hardware.")
    ap.add_argument("--keep-logs", action="store_true", help="keep the temp dir (app log, captures) after the run")
    args = ap.parse_args()
    t0 = time.time()
    res = run(keep_logs=args.keep_logs)
    passed = sum(1 for _, ok, _ in res.rows if ok)
    print(f"\n{passed}/{len(res.rows)} checks passed in {time.time() - t0:.0f}s")
    sys.exit(0 if res.ok else 1)


if __name__ == "__main__":
    main()
