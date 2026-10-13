"""Same-frame comparison: SDK ISP (each Bayer algorithm) vs. our 12-bit pipeline (each cv2 demosaic).

Run with the app stopped:  .venv/bin/python scripts/isp12/sdkcompare.py [camera_index] [exposure_us]
"""
import sys, time, ctypes, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, cv2
import mvsdk
from camera.rawisp import Pipeline12

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 1
exp_us = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
OUT = Path("/tmp/sdkcmp"); OUT.mkdir(exist_ok=True)

dev = mvsdk.CameraEnumerateDevice()[idx]
h = mvsdk.CameraInit(dev, 2, 0)
cap = mvsdk.CameraGetCapability(h)
mvsdk.CameraSetIspOutFormat(h, mvsdk.CAMERA_MEDIA_TYPE_BGR8)
mvsdk.CameraSetTriggerMode(h, 0)
mvsdk.CameraSetAeState(h, 0)
mvsdk.CameraSetExposureTime(h, exp_us)
mvsdk.CameraSetMediaType(h, 1)
mvsdk.CameraPlay(h); time.sleep(0.5)

algs = []
for i in range(6):                       # the capability list reads empty here, so probe by index
    try:
        mvsdk.CameraSetBayerDecAlgorithm(h, 0, i)
        algs.append((i, f"selectable (get->{mvsdk.CameraGetBayerDecAlgorithm(h, 0)})"))
    except mvsdk.CameraException as e:
        print("alg", i, "rejected:", e.error_code)
print("SDK software Bayer algorithms:", algs, "| ISP processor:", mvsdk.CameraGetIspProcessor(h),
      "| device ISP:", cap.sIspCapacity.bDeviceIsp)

W, H = cap.sResolutionRange.iWidthMax, cap.sResolutionRange.iHeightMax
buf8 = mvsdk.CameraAlignMalloc(W * H * 3, 16)

raw, head = mvsdk.CameraGetImageBuffer(h, 3000)
raw_bytes = np.frombuffer((ctypes.c_ubyte * head.uBytes).from_address(raw), np.uint8).copy()
sdk = {}
for i, name in algs:
    mvsdk.CameraSetBayerDecAlgorithm(h, 0, i)      # 0 = run on the PC
    mvsdk.CameraImageProcess(h, raw, buf8, head)   # same raw buffer every time
    a = np.frombuffer((ctypes.c_ubyte * (W * H * 3)).from_address(buf8), np.uint8).reshape(H, W, 3).copy()
    sdk[i] = a
    cv2.imwrite(str(OUT / f"sdk_alg{i}.png"), a)
mvsdk.CameraReleaseImageBuffer(h, raw)
mvsdk.CameraStop(h); mvsdk.CameraUnInit(h)

# SDK output vs SDK output (are the algorithms actually different? any non-determinism?)
res = {"sdk_alg_vs_alg_mae": {f"{a}-{b}": round(float(np.abs(sdk[a].astype(int) - sdk[b].astype(int)).mean()), 3)
                             for a in sdk for b in sdk if a < b}}

pl = Pipeline12(head.iWidth, head.iHeight, cv2.COLOR_BayerGR2BGR)
bayer16 = pl.unpack(raw_bytes).copy()
ours = {"bilinear": cv2.cvtColor(bayer16, cv2.COLOR_BayerGR2BGR)}
for nm, code in (("vng", cv2.COLOR_BayerGR2BGR_VNG), ("ea", cv2.COLOR_BayerGR2BGR_EA)):
    try:
        ours[nm] = cv2.cvtColor(bayer16, code)
    except cv2.error as e:
        res.setdefault("skipped", []).append(f"{nm}: {str(e)[:80]}")


def to8_round(x16):
    return np.clip((x16.astype(np.uint32) + 128) >> 8, 0, 255).astype(np.uint8)


def lap_var(a):
    g = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY) if a.dtype == np.uint8 else a.mean(2)
    return float(cv2.Laplacian(g.astype(np.float32), cv2.CV_32F).var())


def edge_mask(a):
    g = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(g, cv2.CV_16S, ksize=3).__abs__() > 12


res["compare"] = {}
for oname, o16 in ours.items():
    o8 = to8_round(o16)
    for i, a in sdk.items():
        d = o8.astype(int) - a.astype(int)
        m = edge_mask(a)
        res["compare"][f"ours_{oname}_vs_sdk{i}"] = {
            "mae": round(float(np.abs(d).mean()), 3),
            "p99_abs": int(np.percentile(np.abs(d), 99)),
            "mae_edges": round(float(np.abs(d)[m].mean()), 3) if m.any() else None,
            "mae_flat": round(float(np.abs(d)[~m].mean()), 3),
            "bias_bgr": [round(float(d[..., c].mean()), 3) for c in range(3)],
        }
    cv2.imwrite(str(OUT / f"ours_{oname}.png"), o8)
    cv2.imwrite(str(OUT / f"ours_{oname}_16.png"), o16)
res["sharpness_laplacian_var"] = {**{f"sdk{i}": round(lap_var(a), 2) for i, a in sdk.items()},
                                  **{f"ours_{n}": round(lap_var(to8_round(o)), 2) for n, o in ours.items()}}
print(json.dumps(res, indent=1))
