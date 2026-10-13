"""Sweep EVERY SDK ISP control off its default and compare SDK output vs. our identity 12-bit pipeline,
on the same raw frame each time.

Per control we report
  mae_ident : mean |sdk - ours| with ours = bilinear demosaic only        (~0.14 = identical)
  mae_best  : same, after trying flips / rotations of our output          (geometry controls)
  raw_effect: how much the *raw* frame itself moved vs. the default frame (control already baked into raw)
  verdict   : MATCHES (nothing to implement) / DIVERGES (the SDK ISP does something we must replicate)

Run with the app stopped:  .venv/bin/python scripts/isp12/fullsweep.py [camera_index] [exposure_us]
Runtime-only; nothing is saved to the camera and every control is reset after its test.
"""
import sys, time, ctypes, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, cv2
import mvsdk
from camera.rawisp import Pipeline12, to8

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 1
exp_us = int(sys.argv[2]) if len(sys.argv) > 2 else 10000

dev = mvsdk.CameraEnumerateDevice()[idx]
h = mvsdk.CameraInit(dev, 2, 0)
cap = mvsdk.CameraGetCapability(h)
mvsdk.CameraSetIspOutFormat(h, mvsdk.CAMERA_MEDIA_TYPE_BGR8)
mvsdk.CameraSetTriggerMode(h, 0)
mvsdk.CameraSetAeState(h, 0)
mvsdk.CameraSetExposureTime(h, exp_us)
mvsdk.CameraSetMediaType(h, 1)
mvsdk.CameraPlay(h); time.sleep(0.5)
W, H = cap.sResolutionRange.iWidthMax, cap.sResolutionRange.iHeightMax
buf8 = mvsdk.CameraAlignMalloc(W * H * 3, 16)
pl = Pipeline12(W, H, cv2.COLOR_BayerGR2BGR)


def settle():
    time.sleep(0.4)
    for _ in range(3):
        raw, head = mvsdk.CameraGetImageBuffer(h, 3000); mvsdk.CameraReleaseImageBuffer(h, raw)


def frame_pair():
    raw, head = mvsdk.CameraGetImageBuffer(h, 3000)
    rb = np.frombuffer((ctypes.c_ubyte * head.uBytes).from_address(raw), np.uint8).copy()
    mvsdk.CameraImageProcess(h, raw, buf8, head)
    oh, ow = head.iHeight, head.iWidth                       # ISP may rotate -> dims can swap
    sdk = np.frombuffer((ctypes.c_ubyte * (oh * ow * 3)).from_address(buf8), np.uint8).reshape(oh, ow, 3).copy()
    mvsdk.CameraReleaseImageBuffer(h, raw)
    return sdk, to8(pl.process(rb))


TRANSFORMS = {
    "none": lambda a: a,
    "flipH": lambda a: a[:, ::-1],
    "flipV": lambda a: a[::-1],
    "flipHV/rot180": lambda a: a[::-1, ::-1],
    "rot90ccw": lambda a: np.rot90(a, 1),
    "rot270ccw": lambda a: np.rot90(a, 3),
}


def mae(a, b):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()) if a.shape == b.shape else 1e9


def chan_mean(a):
    return a.reshape(-1, 3).mean(0)


def lut_invert():
    inv = (ctypes.c_ushort * 4096)(*[4095 - i for i in range(4096)])
    mvsdk._sdk.CameraSetCustomLut(h, 0, inv)


def lut_inv_reset():
    ident = (ctypes.c_ushort * 4096)(*range(4096))
    mvsdk._sdk.CameraSetCustomLut(h, 0, ident)


S = mvsdk
# name, apply, reset
TESTS = [
    ("RGB gain (150,100,70)", lambda: S.CameraSetGain(h, 150, 100, 70), lambda: S.CameraSetGain(h, 100, 100, 100)),
    ("RGB gain (60,100,160)", lambda: S.CameraSetGain(h, 60, 100, 160), lambda: S.CameraSetGain(h, 100, 100, 100)),
    ("ClrTemp user gain (150,100,70)", lambda: (S.CameraSetClrTempMode(h, 2), S.CameraSetUserClrTempGain(h, 150, 100, 70)),
     lambda: (S.CameraSetUserClrTempGain(h, 100, 100, 100), S.CameraSetClrTempMode(h, 0))),
    ("ClrTemp user matrix (R<->B swap)", lambda: (S.CameraSetClrTempMode(h, 2), S.CameraSetUserClrTempMatrix(h, [0, 0, 1, 0, 1, 0, 1, 0, 0])),
     lambda: (S.CameraSetUserClrTempMatrix(h, [1, 0, 0, 0, 1, 0, 0, 0, 1]), S.CameraSetClrTempMode(h, 0))),
    ("ClrTemp mode preset 1", lambda: (S.CameraSetClrTempMode(h, 1), S.CameraSetPresetClrTemp(h, 1)), lambda: S.CameraSetClrTempMode(h, 0)),
    ("WB auto mode on", lambda: S.CameraSetWbMode(h, 1), lambda: S.CameraSetWbMode(h, 0)),
    ("WB once", lambda: (S.CameraSetWbMode(h, 0), S.CameraSetOnceWB(h), time.sleep(1.5)), lambda: S.CameraSetGain(h, 100, 100, 100)),
    ("Gamma 50", lambda: S.CameraSetGamma(h, 50), lambda: S.CameraSetGamma(h, 100)),
    ("Gamma 250", lambda: S.CameraSetGamma(h, 250), lambda: S.CameraSetGamma(h, 100)),
    ("Contrast 50", lambda: S.CameraSetContrast(h, 50), lambda: S.CameraSetContrast(h, 100)),
    ("Contrast 200", lambda: S.CameraSetContrast(h, 200), lambda: S.CameraSetContrast(h, 100)),
    ("Saturation 0", lambda: S.CameraSetSaturation(h, 0), lambda: S.CameraSetSaturation(h, 100)),
    ("Saturation 200", lambda: S.CameraSetSaturation(h, 200), lambda: S.CameraSetSaturation(h, 100)),
    ("Sharpness 100", lambda: S.CameraSetSharpness(h, 100), lambda: S.CameraSetSharpness(h, 0)),
    ("Noise filter on", lambda: S.CameraSetNoiseFilter(h, 1), lambda: S.CameraSetNoiseFilter(h, 0)),
    ("LUT preset 1", lambda: (S.CameraSetLutMode(h, 1), S.CameraSelectLutPreset(h, 1)), lambda: S.CameraSetLutMode(h, 0)),
    ("LUT user-defined (inverted)", lambda: (lut_invert(), S.CameraSetLutMode(h, 2)), lambda: (S.CameraSetLutMode(h, 0), lut_inv_reset())),
    ("Monochrome", lambda: S.CameraSetMonochrome(h, 1), lambda: S.CameraSetMonochrome(h, 0)),
    ("Invert", lambda: S.CameraSetInverse(h, 1), lambda: S.CameraSetInverse(h, 0)),
    ("Mirror horizontal", lambda: S.CameraSetMirror(h, 0, 1), lambda: S.CameraSetMirror(h, 0, 0)),
    ("Mirror vertical", lambda: S.CameraSetMirror(h, 1, 1), lambda: S.CameraSetMirror(h, 1, 0)),
    ("Rotate 90", lambda: S.CameraSetRotate(h, 1), lambda: S.CameraSetRotate(h, 0)),
    ("Rotate 180", lambda: S.CameraSetRotate(h, 2), lambda: S.CameraSetRotate(h, 0)),
    ("Black level 40", lambda: S.CameraSetBlackLevel(h, 40), lambda: S.CameraSetBlackLevel(h, 0)),
    ("White level 160", lambda: S.CameraSetWhiteLevel(h, 160), lambda: S.CameraSetWhiteLevel(h, 255)),
    ("Analog gain x4 (8 -> 16)", lambda: S.CameraSetAnalogGain(h, 16), lambda: S.CameraSetAnalogGain(h, 4)),
]

settle()
b1 = frame_pair()
base_mean = chan_mean(b1[1])
rows = []
for name, apply, reset in TESTS:
    row = {"control": name}
    try:
        apply(); settle()
        sdk, ours = frame_pair()
        best_t, best_e = min(((t, mae(sdk, f(ours))) for t, f in TRANSFORMS.items()), key=lambda x: x[1])
        e_id = mae(sdk, ours)
        raw_effect = float(np.abs(chan_mean(ours) - base_mean).max())
        row.update(mae_ident=round(e_id, 2) if e_id < 1e8 else None, mae_best=round(best_e, 2), best_transform=best_t,
                   raw_effect_levels=round(raw_effect, 1),
                   verdict="MATCHES" if best_e < 0.5 and best_t == "none" else
                           ("DIVERGES (needs " + best_t + ")" if best_e < 0.5 else "DIVERGES"))
    except Exception as e:                              # setter unsupported on this model, etc.
        row["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            reset(); settle()
        except Exception:
            pass
    rows.append(row)
    print(json.dumps(row), flush=True)

sdk, ours = frame_pair()
print("after_reset mae", round(mae(sdk, ours), 2), "mean vs start", np.round(chan_mean(ours) - base_mean, 1))
mvsdk.CameraStop(h); mvsdk.CameraUnInit(h)
