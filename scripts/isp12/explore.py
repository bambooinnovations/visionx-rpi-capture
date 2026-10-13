"""Dump ISP-relevant camera state and settle the 12-bit packed nibble order.

Run on the Pi with the app stopped:  .venv/bin/python scripts/isp12/explore.py [camera_index]
"""
import sys, time, ctypes
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import numpy as np
import mvsdk

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
dev = mvsdk.CameraEnumerateDevice()[idx]
h = mvsdk.CameraInit(dev, 2, 0)          # same as the app: param group A
def g(name, *a):
    try:
        return getattr(mvsdk, name)(h, *a)
    except Exception as e:
        return f"ERR {e}"

print("black_level", g("CameraGetBlackLevel"))
print("gain RGB (SetGain)", g("CameraGetGain"))
print("user clr temp gain", g("CameraGetUserClrTempGain"))
print("user clr temp matrix", g("CameraGetUserClrTempMatrix"))
print("clr temp mode", g("CameraGetClrTempMode"), "wb auto", g("CameraGetWbMode"))
print("gamma", g("CameraGetGamma"), "contrast", g("CameraGetContrast"), "saturation", g("CameraGetSaturation"))
print("sharpness", g("CameraGetSharpness"), "noise filter", g("CameraGetNoiseFilterState"))
print("lut mode", g("CameraGetLutMode"))
print("analog gain", g("CameraGetAnalogGain"), "analogX", g("CameraGetAnalogGainX"))
print("isp processor", g("CameraGetIspProcessor"))
print("bayer algo(0)", g("CameraGetBayerDecAlgorithm", 0))
for c in range(4):
    lut = g("CameraGetCurrentLut", c)
    if isinstance(lut, (list, tuple)):
        print(f"lut ch{c}: [0]={lut[0]} [256]={lut[256]} [1024]={lut[1024]} [2048]={lut[2048]} [4095]={lut[4095]}")

# --- nibble order: temporal std over static frames, both orders
mvsdk.CameraSetTriggerMode(h, 0)
mvsdk.CameraSetAeState(h, 0)
mvsdk.CameraSetExposureTime(h, 600)   # dark -> low shot noise so the LSB nibble is visible
mvsdk.CameraSetMediaType(h, 1)
mvsdk.CameraPlay(h)
time.sleep(0.5)
frames = []
for _ in range(16):
    raw, head = mvsdk.CameraGetImageBuffer(h, 2000)
    frames.append(bytes((ctypes.c_ubyte * head.uBytes).from_address(raw)))
    mvsdk.CameraReleaseImageBuffer(h, raw)
W, H = head.iWidth, head.iHeight
n = W * H * 3 // 2
def unpack(rb, layout):
    b = np.frombuffer(rb[:n], np.uint8).reshape(-1, 3).astype(np.uint16)
    lo, hi = b[:, 1] & 15, b[:, 1] >> 4
    if layout == "L1 hi8|lo-nib first (GenICam)":
        p0, p1 = (b[:, 0] << 4) | lo, (b[:, 2] << 4) | hi
    elif layout == "L2 hi8|hi-nib first":
        p0, p1 = (b[:, 0] << 4) | hi, (b[:, 2] << 4) | lo
    elif layout == "L3 lo8 + lo-nib high":
        p0, p1 = b[:, 0] | (lo << 8), b[:, 2] | (hi << 8)
    else:
        p0, p1 = b[:, 0] | (hi << 8), b[:, 2] | (lo << 8)
    out = np.empty(W * H, np.uint16)
    out[0::2], out[1::2] = p0, p1
    return out
for layout in ("L1 hi8|lo-nib first (GenICam)", "L2 hi8|hi-nib first", "L3 lo8 + lo-nib high", "L4 lo8 + hi-nib high"):
    stack = np.stack([unpack(f, layout) for f in frames]).astype(np.float32)
    sd = stack.std(0)
    img = stack[0].reshape(H, W)
    rough = np.abs(img[:, 2:] - img[:, :-2]).mean()
    print(f"{layout}: mean {stack.mean():.1f} median temporal std {np.median(sd):.2f} spatial rough {rough:.2f}")
mvsdk.CameraStop(h)
mvsdk.CameraUnInit(h)
