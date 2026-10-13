"""bit_depth camera setting: discovery from the SDK's format list, verified switching, fallbacks (SDK faked)."""
from __future__ import annotations

import ctypes
import types
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from camera import rawformat as rf  # noqa: E402
from camera import rawisp, rawverify  # noqa: E402
import camera.mindvision as mv  # noqa: E402

NAMES = [
    "MONO8", "MONO12", "MONO12_PACKED", "BAYGB8", "BAYGB12_PACKED", "BAYRG8", "BAYRG10", "BAYRG10_PACKED",
    "BAYRG10_MIPI", "BAYRG12", "BAYRG12_PACKED", "BAYBG8", "RGB8", "BGR8",
]


class FakeSdk(types.ModuleType):
    c_ubyte = ctypes.c_ubyte
    CAMERA_STATUS_TIME_OUT = -12

    class CameraException(Exception):
        def __init__(self, code=-1, message="x"):
            super().__init__(message)
            self.error_code, self.message = code, message

    def __init__(self):
        super().__init__("mvsdk")
        for i, n in enumerate(NAMES):
            setattr(self, "CAMERA_MEDIA_TYPE_" + n, 0x01000000 + i)
        self.media_index = 0
        self.calls: list[tuple] = []
        self.state = dict(sharpness=0, noise=0, mono=0, inverse=0, rotate=0, mirror=(0, 0))
        self.frame = None                      # (raw bytes array, head) served by CameraGetImageBuffer
        self.sdk_isp_output = None             # np array written by CameraImageProcess

    def code(self, name):
        return getattr(self, "CAMERA_MEDIA_TYPE_" + name)

    # format switching
    def CameraGetMediaType(self, h): return self.media_index
    def CameraSoftTrigger(self, h): self.calls.append(("soft_trigger",))
    def CameraStop(self, h): self.calls.append(("stop",))
    def CameraPlay(self, h): self.calls.append(("play",))
    def CameraSetMediaType(self, h, idx):
        self.calls.append(("set_media_type", idx)); self.media_index = idx

    # ISP controls read by the raw path
    def CameraGetMonochrome(self, h): return self.state["mono"]
    def CameraGetInverse(self, h): return self.state["inverse"]
    def CameraGetRotate(self, h): return self.state["rotate"]
    def CameraGetMirror(self, h, d): return self.state["mirror"][d]
    def CameraGetSharpness(self, h): return self.state["sharpness"]
    def CameraSetSharpness(self, h, v): self.state["sharpness"] = v
    def CameraGetNoiseFilterState(self, h): return self.state["noise"]
    def CameraSetNoiseFilter(self, h, v): self.state["noise"] = v
    def CameraGetGamma(self, h): return 100
    def CameraGetContrast(self, h): return 100
    def CameraGetSaturation(self, h): return 100
    def CameraGetGain(self, h): return (167, 100, 197)
    def CameraGetFrameStatistic(self, h): return SimpleNamespace(iTotal=0, iLost=0)

    # frames
    def CameraGetImageBuffer(self, h, timeout):
        buf, head = self.frame
        return ctypes.addressof(buf), SimpleNamespace(**vars(head))
    def CameraReleaseImageBuffer(self, h, raw): self.calls.append(("release",))
    def CameraImageProcess(self, h, raw, out, head):
        a = self.sdk_isp_output
        ctypes.memmove(out, a.ctypes.data, a.nbytes)
        head.uiMediaType = self.code("MONO8") if a.ndim == 2 or a.shape[-1] == 1 else self.code("BGR8")
        head.iHeight, head.iWidth = a.shape[:2]
        head.uBytes = a.nbytes


@pytest.fixture()
def sdk(monkeypatch, tmp_path):
    fake = FakeSdk()
    monkeypatch.setattr(mv, "mvsdk", fake, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "mvsdk", fake)
    monkeypatch.setattr(mv, "_raw_registry_cache", None)
    monkeypatch.setattr(mv, "_VERDICTS_PATH", tmp_path / "verdicts.json")
    return fake


def make_cap(sdk, names, mono=False):
    descs = [SimpleNamespace(iMediaType=sdk.code(n)) for n in names]
    return SimpleNamespace(iMediaTypeDesc=len(descs), pMediaTypeDesc=descs,
                           sIspCapacity=SimpleNamespace(bMonoSensor=int(mono)))


def make_cam(sdk, names, mono=False, serial="SN123"):
    cam = mv.MindVisionCamera(1)
    cam._h_camera = 7
    cam._cap = make_cap(sdk, names, mono)
    cam._dev_info = SimpleNamespace(GetSn=lambda: serial)
    return cam


def ok(pattern="GB", mae=0.2):
    return rawverify.Verdict(True, "matches the SDK", pattern, mae)


def bad(reason="decode differs"):
    return rawverify.Verdict(False, reason)


# ── discovery ───────────────────────────────────────────────────────────────────────────────────────────────

def test_setting_key_registered():
    assert "bit_depth" in mv.SETTING_KEYS


@pytest.mark.parametrize("names,expected", [
    (["BAYGB8", "BAYGB12_PACKED"], [8, 12]),                      # MV-SUS507GC
    (["BAYRG8"], [8]),                                            # MV-SUA2000C as its spec sheet lists it
    (["BAYRG8", "BAYRG10_PACKED", "BAYRG12"], [8, 10, 12]),
    (["MONO8", "MONO12_PACKED"], [8, 12]),                        # a mono model
    (["RGB8", "BGR8"], [8]),                                      # nothing we can decode -> still 8-bit
])
def test_options_come_from_the_formats_the_camera_lists(sdk, names, expected):
    assert mv.bit_depth_options(make_cap(sdk, names)) == expected


def test_read_bit_depth_follows_the_selected_media_type(sdk):
    cap = make_cap(sdk, ["BAYRG8", "BAYRG10_PACKED", "BAYRG12"])
    for idx, depth in enumerate([8, 10, 12]):
        sdk.media_index = idx
        assert mv.read_bit_depth(7, cap) == depth


def test_an_unknown_media_type_reads_as_8_bit(sdk):
    cap = make_cap(sdk, ["RGB8", "BAYRG12"])
    sdk.media_index = 0
    assert mv.read_bit_depth(7, cap) == 8


# ── _apply_bit_depth guards ──────────────────────────────────────────────────────────────────────────────────

def test_apply_rejects_unsupported_and_garbage_values(sdk):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG12"])
    for bad_value in ("abc", None, 16, 10):
        assert mv._apply_bit_depth(7, cam._cap, bad_value, cam)
    assert sdk.calls == []


def test_apply_same_depth_is_a_no_op(sdk):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG12"])
    assert mv._apply_bit_depth(7, cam._cap, 8, cam) is None
    assert sdk.calls == []


def test_apply_is_refused_in_hardware_trigger_mode(sdk):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG12"])
    cam._mode = mv.CameraMode.HARDWARE_TRIGGER
    err = mv._apply_bit_depth(7, cam._cap, 12, cam)
    assert err and "hardware-trigger" in err and sdk.calls == []


def test_apply_above_8_bits_needs_the_camera_object(sdk):
    cap = make_cap(sdk, ["BAYRG8", "BAYRG12"])
    assert "camera object" in mv._apply_bit_depth(7, cap, 12, None)


# ── switching with verification ──────────────────────────────────────────────────────────────────────────────

def test_switch_selects_the_media_type_by_format_not_position(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYRG12", "BAYRG8"])                    # 12-bit listed first
    sdk.media_index = 1
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: ok("RG"))
    assert cam.set_bit_depth(12) is None
    assert sdk.calls == [("stop",), ("set_media_type", 0), ("play",)]
    assert cam._raw_choice[0].name == "BAYRG12" and cam._raw_choice[1] == "RG"


def test_a_verified_format_is_remembered_and_not_checked_again(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    calls = []
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: calls.append(fmt.name) or ok("GB"))
    assert cam.set_bit_depth(12) is None
    assert cam.set_bit_depth(8) is None and cam._raw_choice is None
    assert cam.set_bit_depth(12) is None
    assert calls == ["BAYGB12_PACKED"]


def test_a_verdict_survives_a_restart(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: ok("GB", 0.31))
    assert cam.set_bit_depth(12) is None
    again = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])           # a new process: no in-memory verdicts
    monkeypatch.setattr(again, "_verify_format", lambda fmt: pytest.fail("should reuse the saved verdict"))
    assert again.set_bit_depth(12) is None and again._raw_choice[1] == "GB"


def test_verdicts_are_per_camera_serial(sdk, monkeypatch):
    a = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"], serial="A")
    monkeypatch.setattr(a, "_verify_format", lambda fmt: ok("GB"))
    a.set_bit_depth(12)
    b = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"], serial="B")
    seen = []
    monkeypatch.setattr(b, "_verify_format", lambda fmt: seen.append(1) or ok("GB"))
    b.set_bit_depth(12)
    assert seen == [1]


def test_a_failed_check_restores_the_previous_format_and_explains_why(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG12"])
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: bad("decoded image differs from the SDK's"))
    err = cam.set_bit_depth(12)
    assert err and "differs from the SDK" in err and "12bit" in err
    assert sdk.media_index == 0 and cam._raw_choice is None
    assert sdk.calls[-3:] == [("stop",), ("set_media_type", 0), ("play",)]


def test_failures_are_not_persisted_so_a_retry_can_succeed(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG12"])
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: bad("scene too flat"))
    assert cam.set_bit_depth(12)
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: ok("RG"))
    assert cam.set_bit_depth(12) is None


def test_the_next_candidate_is_tried_when_the_preferred_format_fails(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG10", "BAYRG10_MIPI", "BAYRG10_PACKED"])
    verdicts = {"BAYRG10_PACKED": bad("wrong packing"), "BAYRG10_MIPI": ok("RG"), "BAYRG10": ok("RG")}
    order = []
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: order.append(fmt.name) or verdicts[fmt.name])
    assert cam.set_bit_depth(10) is None
    assert order == ["BAYRG10_PACKED", "BAYRG10_MIPI"]            # packed first, then MIPI; the plain one never needed
    assert cam._raw_choice[0].name == "BAYRG10_MIPI"


def test_a_corrected_bayer_pattern_is_what_gets_used(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG12"])
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: ok("GB"))   # the SDK said RG, the frame says GB
    cam.set_bit_depth(12)
    assert cam._raw_choice[1] == "GB"


def test_a_crash_during_verification_is_reported_not_raised(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYRG8", "BAYRG12"])
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: (_ for _ in ()).throw(RuntimeError("camera unplugged")))
    err = cam.set_bit_depth(12)
    assert err and "camera unplugged" in err and sdk.media_index == 0


# ── status for the settings page ─────────────────────────────────────────────────────────────────────────────

def test_status_reports_unverified_verified_and_failed(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    st = cam.raw_format_status()
    assert list(st) == ["12"] and st["12"]["state"] == "unverified"
    assert st["12"]["low_bits_hardware_verified"] is True          # gev12: confirmed on the MV-SUS507GC
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: ok())
    cam.set_bit_depth(12)
    assert cam.raw_format_status()["12"]["state"] == "verified"
    cam2 = make_cam(sdk, ["BAYRG8", "BAYRG10_PACKED"], serial="OTHER")
    assert cam2.raw_format_status()["10"]["low_bits_hardware_verified"] is False   # gev10: published layout only
    monkeypatch.setattr(cam2, "_verify_format", lambda fmt: bad("nope"))
    cam2.set_bit_depth(10)
    assert cam2.raw_format_status()["10"]["state"] == "failed"


def test_status_has_nothing_for_an_8_bit_only_camera(sdk):
    assert make_cam(sdk, ["BAYRG8"]).raw_format_status() == {}


# ── when the raw path is used ────────────────────────────────────────────────────────────────────────────────

def twelve(sdk, names=("BAYGB8", "BAYGB12_PACKED")):
    cam = make_cam(sdk, list(names))
    sdk.media_index = list(names).index("BAYGB12_PACKED")
    fmt = next(f for _, f in mv._camera_formats(cam._cap) if f.name == "BAYGB12_PACKED")
    cam._raw_choice = (fmt, "GB")
    return cam, fmt


def test_8_bit_uses_the_sdk_path(sdk):
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    assert cam._use_raw_path() is False


def test_12_bit_uses_the_raw_path(sdk):
    cam, _ = twelve(sdk)
    assert cam._use_raw_path() is True


def test_hardware_trigger_mode_stays_on_the_sdk_path(sdk):
    cam, _ = twelve(sdk)
    cam._mode = mv.CameraMode.HARDWARE_TRIGGER
    assert cam._use_raw_path() is False


def test_a_mono_sensor_with_sharpness_falls_back_to_the_sdk_path(sdk):
    cam = make_cam(sdk, ["MONO8", "MONO12_PACKED"], mono=True)
    sdk.media_index = 1
    fmt = next(f for _, f in mv._camera_formats(cam._cap) if f.name == "MONO12_PACKED")
    cam._raw_choice = (fmt, None)
    assert cam._use_raw_path() is True
    sdk.state["sharpness"] = 30
    assert cam._use_raw_path() is False


def test_an_unverified_format_the_camera_came_up_in_is_checked_on_first_use(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    sdk.media_index = 1                                           # saved parameters left it in 12-bit
    seen = []
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: seen.append(fmt.name) or ok("GB"))
    assert cam._use_raw_path() is True and seen == ["BAYGB12_PACKED"]
    assert cam._use_raw_path() is True and seen == ["BAYGB12_PACKED"]


def test_a_format_that_fails_its_first_check_means_8_bit_captures(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    sdk.media_index = 1
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: bad("nope"))
    assert cam._use_raw_path() is False


# ── the grab itself ──────────────────────────────────────────────────────────────────────────────────────────

def serve(sdk, fmt, vals, w=64, h=48, **head_over):
    raw = rf.pack_numpy(vals, fmt)
    buf = (ctypes.c_ubyte * raw.size).from_buffer_copy(raw.tobytes())
    head = SimpleNamespace(uiMediaType=fmt.code, uBytes=raw.size, iWidth=w, iHeight=h,
                           iGamma=0, iContrast=0, iSaturation=0, fRgain=0.0, fGgain=0.0, fBgain=0.0)
    for k, v in head_over.items():
        setattr(head, k, v)
    sdk.frame = (buf, head)
    return buf


def test_grab_develops_a_raw_frame_to_16_bit_and_fills_the_header(sdk):
    cam, fmt = twelve(sdk)
    vals = np.random.default_rng(1).integers(0, 4096, (48, 64), dtype=np.uint16)
    serve(sdk, fmt, vals)
    img, head = cam._grab_frame_raw()
    expected = cv2.cvtColor((vals << 4).astype(np.uint16), cv2.COLOR_BayerGR2BGR)
    assert img.dtype == np.uint16 and (img == expected).all()
    assert (head.iGamma, head.iContrast, head.iSaturation) == (100, 100, 100)
    assert (head.fRgain, head.fGgain, head.fBgain) == (1.67, 1.0, 1.97)
    assert ("release",) in sdk.calls


def test_grab_hands_back_a_copy_not_the_pipeline_buffer(sdk):
    cam, fmt = twelve(sdk)
    serve(sdk, fmt, np.full((48, 64), 100, np.uint16))
    a, _ = cam._grab_frame_raw()
    serve(sdk, fmt, np.full((48, 64), 3000, np.uint16))
    cam._grab_frame_raw()
    assert (a[8:-8, 8:-8] == 100 << 4).all()


def test_a_frame_that_is_not_what_was_verified_goes_through_the_sdk_isp_instead(sdk, monkeypatch):
    cam, fmt = twelve(sdk)
    cam._raw_verdicts[fmt.name] = ok()
    vals = np.random.default_rng(1).integers(0, 4096, (48, 64), dtype=np.uint16)
    serve(sdk, fmt, vals, uBytes=48 * 64 * 2)                     # the camera is suddenly sending something else
    sdk.sdk_isp_output = np.full((48, 64, 3), 77, np.uint8)
    cam._frame_buffer = ctypes.addressof((ctypes.c_ubyte * (48 * 64 * 3))())
    keep = (ctypes.c_ubyte * (48 * 64 * 3))()
    cam._frame_buffer = ctypes.addressof(keep)
    img, head = cam._grab_frame_raw()
    assert img.dtype == np.uint8 and (img == 77).all()
    assert cam._raw_choice is None and fmt.name not in cam._raw_verdicts
    assert mv._load_verdict("SN123", fmt.name) is None


def test_grab_timeouts_return_none(sdk):
    cam, fmt = twelve(sdk)

    def boom(h, t):
        raise sdk.CameraException(sdk.CAMERA_STATUS_TIME_OUT, "timeout")
    sdk.CameraGetImageBuffer = boom
    assert cam._grab_frame_raw() == (None, None)


# ── the saved verdict file ───────────────────────────────────────────────────────────────────────────────────

def test_verdict_file_roundtrip_and_forget(sdk):
    assert mv._load_verdict("S", "X") is None
    mv._save_verdict("S", "X", ok("RG", 0.4))
    v = mv._load_verdict("S", "X")
    assert v.ok and v.pattern == "RG" and v.mae == 0.4
    mv._save_verdict("S", "Y", bad("no"))                          # failures are never saved
    assert mv._load_verdict("S", "Y") is None
    mv._forget_verdict("S", "X")
    assert mv._load_verdict("S", "X") is None


def test_a_corrupt_verdict_file_is_ignored(sdk, tmp_path):
    (tmp_path / "verdicts.json").write_text("{not json")
    assert mv._load_verdict("S", "X") is None
    mv._save_verdict("S", "X", ok())                               # and it gets repaired by the next save
    assert mv._load_verdict("S", "X") is not None


# ── software-trigger mode ────────────────────────────────────────────────────────────────────────────────────

def test_verification_frames_are_requested_when_the_camera_waits_for_a_trigger(sdk):
    cam, fmt = twelve(sdk)
    cam._streaming = False
    cam._mode = mv.CameraMode.CAPTURE
    vals = np.random.default_rng(3).integers(0, 4096, (48, 64), dtype=np.uint16)
    serve(sdk, fmt, vals)
    sdk.sdk_isp_output = np.zeros((48, 64, 3), np.uint8)
    keep = (ctypes.c_ubyte * (48 * 64 * 3))()
    cam._frame_buffer = ctypes.addressof(keep)
    cam._verification_grab()
    assert sdk.calls[0] == ("soft_trigger",)


def test_no_trigger_is_sent_while_streaming(sdk):
    cam, fmt = twelve(sdk)
    cam._streaming = True
    serve(sdk, fmt, np.zeros((48, 64), np.uint16))
    cam._flush_frames(2)
    assert ("soft_trigger",) not in sdk.calls


def test_a_capture_decides_raw_vs_sdk_before_it_triggers(sdk, monkeypatch):
    """The first use of a raw format runs a check that takes frames; it must not steal the capture's own frame."""
    import inspect
    src = inspect.getsource(mv.MindVisionCamera.capture_image)
    assert src.index("self._raw_path_options()") < src.index("CameraSoftTrigger")


# ── inconclusive checks, verdict file safety ─────────────────────────────────────────────────────────────────

def test_an_inconclusive_check_is_reported_as_retry_not_failed(sdk, monkeypatch):
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    monkeypatch.setattr(cam, "_verify_format",
                        lambda fmt: rawverify.Verdict(False, "scene too flat", inconclusive=True))
    assert cam.set_bit_depth(12) is not None
    assert cam.raw_format_status()["12"]["state"] == "retry"


def test_a_corrupt_verdict_file_is_moved_aside_not_lost(sdk, tmp_path):
    (tmp_path / "verdicts.json").write_text("{not json")
    mv._save_verdict("S", "X", ok())
    assert (tmp_path / "verdicts.corrupt").read_text() == "{not json"
    assert mv._load_verdict("S", "X") is not None
    assert not (tmp_path / "verdicts.tmp").exists()


# ── lock order: _lock, then _exposure_lock ─────────────────────────────────────────────────────────────────

def test_changing_bit_depth_does_not_deadlock_against_a_capture(sdk, monkeypatch):
    """capture_image holds _lock and then wants _exposure_lock; a settings change that switches the format takes
    both. If the settings side took them in the other order the two threads would wait on each other forever."""
    import threading
    cam = make_cam(sdk, ["BAYGB8", "BAYGB12_PACKED"])
    monkeypatch.setattr(cam, "_verify_format", lambda fmt: ok())
    stop = threading.Event()

    def capture_like():
        while not stop.is_set():
            with cam._lock:
                with cam._exposure_lock:
                    pass

    t = threading.Thread(target=capture_like, daemon=True)
    t.start()

    def switch():
        for depth in (12, 8) * 40:
            cam.apply_production({"bit_depth": depth})

    s = threading.Thread(target=switch, daemon=True)
    s.start()
    s.join(timeout=20)
    stop.set()
    t.join(timeout=5)
    assert not s.is_alive(), "apply_production deadlocked against a capture"
