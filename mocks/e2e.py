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


def scenario_wrap_up(h: Harness, r: Results) -> None:
    print("wrap up")
    h.arduino.set_speed(0.0)
    st = h.status()
    r.check("no capture failed during the run", st["captures_failed"] == 0, f"{st['captures_ok']} captures ok")
    resp = h.api("POST", "/api/decoder/stop")
    r.check("decoder stops cleanly", resp.ok, resp.text[:80])


SCENARIOS = [scenario_start_and_configure, scenario_roll, scenario_new_roll, scenario_serial_drop,
             scenario_upload_failures, scenario_wrap_up]


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
