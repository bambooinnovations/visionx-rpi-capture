"""Guided white balance: the box maths, the camera's tune sequence (SDK faked) and the /white-balance routes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
flask = pytest.importorskip("flask")

from camera import wbtune  # noqa: E402
import camera.mindvision as mv  # noqa: E402

BOX = {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}


def frame_bgr(r, g, b, size=(100, 120)):
    """A flat frame; `size` is (height, width)."""
    out = np.empty((*size, 3), np.uint8)
    out[..., 0], out[..., 1], out[..., 2] = b, g, r
    return out


# ── wbtune: the box maths ─────────────────────────────────────────────────────────────────────────────────────

def test_box_stats_reads_only_the_box():
    frame = frame_bgr(10, 10, 10)
    h, w = frame.shape[:2]
    frame[h // 4:h * 3 // 4, w // 4:w * 3 // 4] = (50, 150, 100)   # B, G, R
    s = wbtune.box_stats(frame, BOX)
    assert (round(s["r"]), round(s["g"]), round(s["b"])) == (100, 150, 50)
    assert s["clipped"] == 0.0


def test_box_stats_reports_clipping():
    s = wbtune.box_stats(frame_bgr(255, 200, 200), BOX)
    assert s["clipped"] == 1.0
    assert wbtune.classify(s)["state"] == "clipped"


@pytest.mark.parametrize("g, state", [(10, "dark"), (120, "low"), (200, "ok"), (230, "high")])
def test_classify_by_green_level(g, state):
    assert wbtune.classify({"r": g, "g": g, "b": g, "clipped": 0.0})["state"] == state


@pytest.mark.parametrize("box", [
    None, {}, {"x": 0, "y": 0, "w": "a", "h": 1}, {"x": 0, "y": 0, "w": 0, "h": 1},
    {"x": 1.2, "y": 0, "w": 0.1, "h": 0.1}, {"x": float("nan"), "y": 0, "w": 1, "h": 1},
])
def test_bad_boxes_are_rejected(box):
    with pytest.raises(ValueError):
        wbtune.parse_box(box)


def test_tiny_box_is_rejected_for_stats():
    with pytest.raises(ValueError, match="too small"):
        wbtune.box_stats(frame_bgr(200, 200, 200), {"x": 0, "y": 0, "w": 0.05, "h": 0.05})


def test_mono_frame_is_rejected():
    with pytest.raises(ValueError, match="colour"):
        wbtune.box_stats(np.zeros((100, 100), np.uint8), BOX)


def test_box_to_pixels_is_inside_the_image_and_at_least_one_pixel():
    assert wbtune.box_to_pixels({"x": 0.5, "y": 0.25, "w": 0.25, "h": 0.5}, 8240, 5628) == (4120, 1407, 2060, 2814)
    x, y, w, h = wbtune.box_to_pixels({"x": 0.999999, "y": 0.999999, "w": 0.5, "h": 0.5}, 100, 80)
    assert (x + w <= 100) and (y + h <= 80) and w >= 1 and h >= 1


# ── MindVisionCamera.wb_tune with a faked SDK ─────────────────────────────────────────────────────────────────

class FakeSdk:
    """Just enough SDK for the guided white balance. A cast scene: red and blue read low until the gains fix it."""
    CameraException = Exception

    def __init__(self, presets=(("D50", 0), ("D65", 1), ("A", 2))):
        self.calls = []
        self.gain = (100, 100, 100)
        self.clr = (0, 0)           # (mode, preset index)
        self.wb_auto = 1
        self.window = (0, 0, 0, 0)
        self.once_pending = False
        self.once_gain = (118, 100, 131)
        self.presets = presets

    def _rec(self, name, *a):
        self.calls.append((name, *a))

    def CameraGetMonochrome(self, h): return 0
    def CameraGetGain(self, h): return self.gain
    def CameraGetWbMode(self, h): return self.wb_auto
    def CameraGetWbWindow(self, h): return self.window
    def CameraGetClrTempMode(self, h): return self.clr[0]
    def CameraGetPresetClrTemp(self, h): return self.clr[1]
    def CameraGetImageResolution(self, h): return SimpleNamespace(iWidth=1200, iHeight=1000)

    def CameraSetWbMode(self, h, auto): self._rec("wb_mode", bool(auto)); self.wb_auto = int(bool(auto))
    def CameraSetPresetClrTemp(self, h, i): self._rec("preset", i); self.clr = (self.clr[0], i)
    def CameraSetClrTempMode(self, h, m): self._rec("clr_mode", m); self.clr = (m, self.clr[1])
    def CameraSetWbWindow(self, h, *w): self._rec("window", *w); self.window = w
    def CameraSetGain(self, h, r, g, b): self._rec("gain", r, g, b); self.gain = (r, g, b)
    def CameraSetOnceWB(self, h): self._rec("once"); self.once_pending = True

    def scene_frame(self):
        """What a grab returns right now: the white patch as the current gains render it."""
        if self.once_pending:                  # OnceWB takes effect on the next frame received
            self.once_pending = False
            self.gain = self.once_gain
        r, g, b = (min(255, int(base * gain / 100)) for base, gain in zip((170, 200, 153), self.gain))
        return frame_bgr(r, g, b, (100, 120))


def make_camera(sdk, monkeypatch, mode=mv.CameraMode.STREAM):
    cam = mv.MindVisionCamera(0)
    cam._h_camera = 1
    cam._mode = mode
    descs = [SimpleNamespace(iIndex=i, GetDescription=lambda n=n: n) for n, i in sdk.presets]
    cam._cap = SimpleNamespace(iClrTempDesc=len(descs), pClrTempDesc=descs)
    monkeypatch.setattr(mv, "mvsdk", sdk, raising=False)
    monkeypatch.setattr(cam, "_grab_frame", lambda timeout_ms=1000: (sdk.scene_frame(), None))
    monkeypatch.setattr(cam, "_trigger_if_needed", lambda: None)
    monkeypatch.setattr(cam, "exposure_grab_timeout_ms", lambda: 100)
    return cam


def test_tune_runs_the_vendor_sequence_and_leaves_a_draft(monkeypatch):
    sdk = FakeSdk()
    cam = make_camera(sdk, monkeypatch)
    result = cam.wb_tune(BOX)

    names = [c[0] for c in sdk.calls]
    # manual WB, D65 selected, window set, gains neutralised, then the one-push, in that order
    assert names == ["wb_mode", "preset", "clr_mode", "window", "gain", "once"]
    assert sdk.calls[0] == ("wb_mode", False)
    assert sdk.calls[1] == ("preset", 1) and sdk.calls[2] == ("clr_mode", 1)   # D65 is index 1, found by name
    # box (0.25, 0.25, 0.5, 0.5) of the 1200x1000 resolution
    assert sdk.calls[3] == ("window", 300, 250, 600, 500)
    assert sdk.calls[4] == ("gain", 100, 100, 100)

    assert result["gains"] == {"r_gain": 118, "g_gain": 100, "b_gain": 131}
    assert result["before"]["r_gain"] == 100 and result["before"]["color_temp"] == {"mode": 0, "index": None}
    assert result["neutral"] is True and result["spread"] <= 2
    assert result["stats"]["exposure"]["state"] == "ok"
    assert cam._draft_dirty is True


def test_tune_reports_a_patch_that_is_still_tinted(monkeypatch):
    sdk = FakeSdk()
    sdk.once_gain = (100, 100, 100)      # the camera balanced on something else
    result = make_camera(sdk, monkeypatch).wb_tune(BOX)
    assert result["neutral"] is False and result["spread"] > wbtune.NEUTRAL_SPREAD_WARN


def test_tune_without_a_d65_preset_changes_nothing(monkeypatch):
    sdk = FakeSdk(presets=(("D50", 0), ("A", 2)))
    cam = make_camera(sdk, monkeypatch)
    with pytest.raises(RuntimeError, match="no D65"):
        cam.wb_tune(BOX)
    assert sdk.calls == []


def test_tune_restores_the_camera_when_it_fails_midway(monkeypatch):
    sdk = FakeSdk()
    sdk.gain, sdk.window, sdk.clr, sdk.wb_auto = (110, 100, 90), (1, 2, 3, 4), (1, 0), 1
    cam = make_camera(sdk, monkeypatch)

    def boom():
        raise RuntimeError("camera gone")
    sdk.CameraSetOnceWB = lambda h: boom()
    with pytest.raises(RuntimeError, match="camera gone"):
        cam.wb_tune(BOX)
    assert sdk.gain == (110, 100, 90)
    assert sdk.clr == (1, 0)                # previous preset and mode are back
    assert sdk.window == (1, 2, 3, 4) and sdk.wb_auto == 1
    assert cam._draft_dirty is False


def test_tune_refuses_in_hardware_trigger_mode(monkeypatch):
    sdk = FakeSdk()
    cam = make_camera(sdk, monkeypatch, mode=mv.CameraMode.HARDWARE_TRIGGER)
    with pytest.raises(RuntimeError, match="hardware-trigger"):
        cam.wb_tune(BOX)
    assert sdk.calls == []


def test_tune_refuses_a_monochrome_camera(monkeypatch):
    sdk = FakeSdk()
    sdk.CameraGetMonochrome = lambda h: 1
    with pytest.raises(RuntimeError, match="monochrome"):
        make_camera(sdk, monkeypatch).wb_tune(BOX)


def test_tune_rejects_a_bad_box_before_touching_the_camera(monkeypatch):
    sdk = FakeSdk()
    with pytest.raises(ValueError):
        make_camera(sdk, monkeypatch).wb_tune({"x": 0, "y": 0, "w": 0, "h": 0})
    assert sdk.calls == []


def test_measure_returns_stats_and_an_exposure_verdict(monkeypatch):
    sdk = FakeSdk()
    stats, frame = make_camera(sdk, monkeypatch).wb_measure(BOX)
    assert round(stats["g"]) == 200 and stats["exposure"]["state"] == "ok"
    assert frame.shape == (100, 120, 3)


def test_set_color_temp_validates_and_applies(monkeypatch):
    sdk = FakeSdk()
    cam = make_camera(sdk, monkeypatch)
    cam.set_wb_color_temp(1, 2)
    assert sdk.clr == (1, 2)
    cam.set_wb_color_temp(0, None)
    assert sdk.clr[0] == 0
    with pytest.raises(ValueError):
        cam.set_wb_color_temp(1, 99)
    with pytest.raises(ValueError):
        cam.set_wb_color_temp(7, None)


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("PIL")
    import blueprints.mindvision as bp_mod
    sdk = FakeSdk()
    cam = make_camera(sdk, monkeypatch)
    cam._mono = False
    app = flask.Flask(__name__)
    app.register_blueprint(bp_mod.create_blueprint({0: cam}, serial_listener=object()))
    c = app.test_client()
    c.cam, c.sdk = cam, sdk
    return c


def test_guide_route_names_the_d65_preset(client):
    body = client.get("/api/cameras/white-balance/guide").get_json()
    assert body["color_temps"]["d65_index"] == 1
    assert body["g_target"] == 200 and body["monochrome"] is False


def test_measure_route_returns_stats_and_an_image(client):
    body = client.post("/api/cameras/white-balance/measure", json={"box": BOX}).get_json()
    assert round(body["stats"]["g"]) == 200
    assert body["image"].startswith("data:image/jpeg;base64,")


def test_tune_route_returns_the_gains(client):
    body = client.post("/api/cameras/white-balance/tune", json={"box": BOX}).get_json()
    assert body["gains"] == {"r_gain": 118, "g_gain": 100, "b_gain": 131}
    assert body["neutral"] is True


def test_route_errors_are_clean(client):
    assert client.post("/api/cameras/white-balance/tune", json={}).status_code == 400
    assert client.post("/api/cameras/white-balance/measure", json={"box": {"x": 0, "y": 0, "w": 0.01, "h": 0.01}}).status_code == 400
    assert client.post("/api/cameras/white-balance/color-temp", json={"mode": "x"}).status_code == 400
    assert client.post("/api/cameras/white-balance/color-temp", json={"mode": 1, "index": 99}).status_code == 400
    assert client.post("/api/cameras/white-balance/tune?camera_id=5", json={"box": BOX}).status_code == 404
    client.cam._mode = mv.CameraMode.HARDWARE_TRIGGER
    assert client.post("/api/cameras/white-balance/tune", json={"box": BOX}).status_code == 409


def test_color_temp_route_puts_a_choice_back(client):
    r = client.post("/api/cameras/white-balance/color-temp", json={"mode": 1, "index": 2})
    assert r.status_code == 200 and client.sdk.clr == (1, 2)
