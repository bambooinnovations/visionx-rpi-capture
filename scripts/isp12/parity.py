"""Parity test: for each SDK ISP control we implemented, set it in the SDK and in our pipeline, grab one raw
12-bit frame, run both on it, and report mean |sdk - ours| (8-bit levels; ~0.14 is the bilinear-truncation floor).

Run with the app stopped:  .venv/bin/python scripts/isp12/parity.py [camera_index] [exposure_us]
"""
import sys, time, ctypes, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, cv2
import mvsdk as S
from camera.rawisp import Pipeline12, to8

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 1
exp_us = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
h = S.CameraInit(S.CameraEnumerateDevice()[idx], 2, 0)
cap = S.CameraGetCapability(h)
S.CameraSetIspOutFormat(h, S.CAMERA_MEDIA_TYPE_BGR8)
S.CameraSetTriggerMode(h, 0); S.CameraSetAeState(h, 0); S.CameraSetExposureTime(h, exp_us)
S.CameraSetMediaType(h, 1); S.CameraPlay(h); time.sleep(0.5)
W, H = cap.sResolutionRange.iWidthMax, cap.sResolutionRange.iHeightMax
buf = S.CameraAlignMalloc(W * H * 3, 16)
pl = Pipeline12(W, H, cv2.COLOR_BayerGR2BGR)


def settle():
    time.sleep(0.4)
    for _ in range(3):
        r, hd = S.CameraGetImageBuffer(h, 3000); S.CameraReleaseImageBuffer(h, r)


def grab_raw_and_sdk():
    r, hd = S.CameraGetImageBuffer(h, 3000)
    rb = np.frombuffer((ctypes.c_ubyte * hd.uBytes).from_address(r), np.uint8).copy()
    S.CameraImageProcess(h, r, buf, hd)
    oh, ow = hd.iHeight, hd.iWidth
    sdk = np.frombuffer((ctypes.c_ubyte * (oh * ow * 3)).from_address(buf), np.uint8).reshape(oh, ow, 3).copy()
    S.CameraReleaseImageBuffer(h, r)
    return rb, sdk


def mae(a, b):
    return round(float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()), 3) if a.shape == b.shape else f"shape {a.shape} vs {b.shape}"


def cur_lut(ch):
    return list(S.CameraGetCurrentLut(h, ch))


def inv_lut():
    return [4095 - i for i in range(4096)]


def set_user_lut(lut):
    S._sdk.CameraSetCustomLut(h, 0, (ctypes.c_ushort * 4096)(*lut))
    S.CameraSetLutMode(h, 2)


results = []


def run(name, sdk_apply, sdk_reset, opts, extra=None):
    try:
        sdk_apply(); settle()
        o = dict(opts)
        if callable(o.get("lut")):
            o["lut"] = o["lut"]()
        pl.set_options(**o)
        rb, sdk = grab_raw_and_sdk()
        ours = to8(pl.process(rb))
        row = {"test": name, "mae": mae(sdk, ours)}
        if extra:
            row.update(extra(rb, sdk))
    except Exception as e:
        row = {"test": name, "error": f"{type(e).__name__}: {e}"}
    finally:
        pl.set_options()
        try:
            sdk_reset(); settle()
        except Exception:
            pass
    results.append(row)
    print(json.dumps(row), flush=True)


reset_geo = lambda: (S.CameraSetMirror(h, 0, 0), S.CameraSetMirror(h, 1, 0), S.CameraSetRotate(h, 0))
settle()
run("baseline (no controls)", lambda: None, lambda: None, {})
run("mirror H", lambda: S.CameraSetMirror(h, 0, 1), reset_geo, {"mirror_h": True})
run("mirror V", lambda: S.CameraSetMirror(h, 1, 1), reset_geo, {"mirror_v": True})
run("mirror H+V", lambda: (S.CameraSetMirror(h, 0, 1), S.CameraSetMirror(h, 1, 1)), reset_geo, {"mirror_h": True, "mirror_v": True})
for k in (1, 2, 3):
    run(f"rotate iRot={k}", lambda k=k: S.CameraSetRotate(h, k), reset_geo, {"rotate": k})
for k in (1, 2, 3):
    run(f"rotate {k} + mirror H", lambda k=k: (S.CameraSetRotate(h, k), S.CameraSetMirror(h, 0, 1)), reset_geo, {"rotate": k, "mirror_h": True})
    run(f"rotate {k} + mirror V", lambda k=k: (S.CameraSetRotate(h, k), S.CameraSetMirror(h, 1, 1)), reset_geo, {"rotate": k, "mirror_v": True})
run("invert", lambda: S.CameraSetInverse(h, 1), lambda: S.CameraSetInverse(h, 0), {"invert": True})
run("monochrome", lambda: S.CameraSetMonochrome(h, 1), lambda: S.CameraSetMonochrome(h, 0), {"mono": True})
run("LUT user-defined (inverted)", lambda: set_user_lut(inv_lut()), lambda: S.CameraSetLutMode(h, 0), {"lut": {"all": inv_lut()}})
for p in (1, 2):
    holder = {}
    def apply_preset(p=p, holder=holder):
        S.CameraSetLutMode(h, 1); S.CameraSelectLutPreset(h, p)
        holder["lut"] = {k: cur_lut(c) for k, c in (("r", 1), ("g", 2), ("b", 3))}
    run(f"LUT preset {p}", apply_preset, lambda: S.CameraSetLutMode(h, 0), {"lut": lambda holder=holder: holder["lut"]})
run("noise filter", lambda: S.CameraSetNoiseFilter(h, 1), lambda: S.CameraSetNoiseFilter(h, 0), {"noise_filter": True})
for lv in (25, 37, 50, 60, 90, 100):
    run(f"sharpness {lv}", lambda lv=lv: S.CameraSetSharpness(h, lv), lambda: S.CameraSetSharpness(h, 0), {"sharpness": lv})
run("noise filter + sharpness 60", lambda: (S.CameraSetNoiseFilter(h, 1), S.CameraSetSharpness(h, 60)),
    lambda: (S.CameraSetNoiseFilter(h, 0), S.CameraSetSharpness(h, 0)), {"noise_filter": True, "sharpness": 60})
run("everything: NF+sharp 60+mono+invert+rot1+mirrorH", lambda: (S.CameraSetNoiseFilter(h, 1), S.CameraSetSharpness(h, 60), S.CameraSetMonochrome(h, 1),
    S.CameraSetInverse(h, 1), S.CameraSetRotate(h, 1), S.CameraSetMirror(h, 0, 1)),
    lambda: (S.CameraSetNoiseFilter(h, 0), S.CameraSetSharpness(h, 0), S.CameraSetMonochrome(h, 0), S.CameraSetInverse(h, 0), reset_geo()),
    {"noise_filter": True, "sharpness": 60, "mono": True, "invert": True, "rotate": 1, "mirror_h": True})
run("invert + mirror H + rotate 1", lambda: (S.CameraSetInverse(h, 1), S.CameraSetMirror(h, 0, 1), S.CameraSetRotate(h, 1)),
    lambda: (S.CameraSetInverse(h, 0), reset_geo()), {"invert": True, "mirror_h": True, "rotate": 1})
run("mono + invert", lambda: (S.CameraSetMonochrome(h, 1), S.CameraSetInverse(h, 1)),
    lambda: (S.CameraSetMonochrome(h, 0), S.CameraSetInverse(h, 0)), {"mono": True, "invert": True})
run("LUT inverted + mono", lambda: (set_user_lut(inv_lut()), S.CameraSetMonochrome(h, 1)),
    lambda: (S.CameraSetLutMode(h, 0), S.CameraSetMonochrome(h, 0)), {"lut": {"all": inv_lut()}, "mono": True})
S.CameraStop(h); S.CameraUnInit(h)
