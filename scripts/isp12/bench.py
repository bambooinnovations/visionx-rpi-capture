"""Benchmark + equivalence test for the optimised pipeline, on the live 5 MP camera.

  A) native vs numpy implementations agree (same raw frame, every option)
  B) offline per-config cost, numpy vs native (4 threads)
  C) live sustained fps: SDK 8-bit, new pipeline in the grab thread, and frame-parallel StreamProcessor combos

Run with the app stopped:  .venv/bin/python scripts/isp12/bench.py [camera_index] [exposure_us]
"""
import sys, time, ctypes, json, queue
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, cv2
import mvsdk as S
import camera.rawisp as P
from camera.rawisp import Pipeline12, StreamProcessor, set_threads

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 1
exp_us = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
h = S.CameraInit(S.CameraEnumerateDevice()[idx], 2, 0)
cap = S.CameraGetCapability(h)
S.CameraSetIspOutFormat(h, S.CAMERA_MEDIA_TYPE_BGR8)
S.CameraSetTriggerMode(h, 0); S.CameraSetAeState(h, 0); S.CameraSetExposureTime(h, exp_us)
W, H = cap.sResolutionRange.iWidthMax, cap.sResolutionRange.iHeightMax
CODE = cv2.COLOR_BayerGR2BGR
print("native lib:", bool(P.load_native()), "| cores:", cv2.getNumberOfCPUs())

# ---------- one real raw frame
S.CameraSetMediaType(h, 1); S.CameraPlay(h); time.sleep(0.5)
raw, head = S.CameraGetImageBuffer(h, 3000)
frame = np.frombuffer((ctypes.c_ubyte * head.uBytes).from_address(raw), np.uint8).copy()
S.CameraReleaseImageBuffer(h, raw)
LUT = [4095 - i for i in range(4096)]
CONFIGS = [
    ("plain", {}, {}),
    ("WB gains (1.3,1,1.2)", {}, {"wb_gains": (1.3, 1.0, 1.2)}),
    ("CCM (full)", {}, {"ccm": [[1.2, -0.1, -0.1], [-0.05, 1.1, -0.05], [0.0, -0.2, 1.2]]}),
    ("black level 40", {}, {"black_level": 40}),
    ("mirror H", {"mirror_h": True}, {}),
    ("rotate 90", {"rotate": 1}, {}),
    ("invert", {"invert": True}, {}),
    ("monochrome", {"mono": True}, {}),
    ("LUT custom", {"lut": {"all": LUT}}, {}),
    ("noise filter", {"noise_filter": True}, {}),
    ("sharpness 60", {"sharpness": 60}, {}),
    ("everything", {"mirror_h": True, "rotate": 1, "invert": True, "mono": True, "noise_filter": True, "sharpness": 60,
                    "lut": {"all": LUT}}, {"wb_gains": (1.3, 1.0, 1.2), "ccm": [[1.2, -0.1, -0.1], [-0.05, 1.1, -0.05], [0, -0.2, 1.2]]}),
]


def build(native, opts, color):
    p = Pipeline12(W, H, CODE, native=native, **color)
    p.set_options(**opts)
    return p


def timeit(p, n=10):
    p.process(frame)
    s = time.perf_counter()
    for _ in range(n):
        p.process(frame)
    return (time.perf_counter() - s) / n * 1000


set_threads(4)
res = {"A_equivalence": {}, "B_offline_ms": {}}
for name, opts, color in CONFIGS:
    a = build(False, opts, color).process(frame).astype(np.int32)
    b = build(True, opts, color).process(frame).astype(np.int32)
    d = np.abs(a - b)
    inner = d[4:-4, 4:-4] if d.ndim == 2 or d.shape[0] > 8 else d
    res["A_equivalence"][name] = {"max_abs_diff_16bit": int(inner.max()), "mean_abs_diff_16bit": round(float(inner.mean()), 3),
                                  "frac_pixels_differing": round(float((inner != 0).mean()), 4)}
    res["B_offline_ms"][name] = {"numpy": round(timeit(build(False, opts, color)), 1), "native": round(timeit(build(True, opts, color)), 1)}
    print(name, res["A_equivalence"][name], res["B_offline_ms"][name], flush=True)

# stage split for the plain native path
pl = build(True, {}, {})
t = {}
for nm, f in (("unpack", lambda: pl.unpack(frame)), ("demosaic", lambda: pl.demosaic(pl._bayer))):
    f(); s = time.perf_counter()
    for _ in range(15): f()
    t[nm] = round((time.perf_counter() - s) / 15 * 1000, 1)
res["B_plain_native_stage_ms"] = t
print("plain native stages (ms):", t, flush=True)

# ---------- C) live sustained fps
buf8 = S.CameraAlignMalloc(W * H * 3, 16)


def live_sdk8(seconds=5):
    S.CameraStop(h); S.CameraSetMediaType(h, 0); S.CameraPlay(h); time.sleep(0.5)
    n = 0; t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        r, hd = S.CameraGetImageBuffer(h, 2000)
        S.CameraImageProcess(h, r, buf8, hd); S.CameraReleaseImageBuffer(h, r); n += 1
    return round(n / (time.perf_counter() - t0), 1)


def live_raw_only(seconds=5):
    S.CameraStop(h); S.CameraSetMediaType(h, 1); S.CameraPlay(h); time.sleep(0.5)
    n = 0; t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        r, hd = S.CameraGetImageBuffer(h, 2000); S.CameraReleaseImageBuffer(h, r); n += 1
    return round(n / (time.perf_counter() - t0), 1)


def live_sync(opts, seconds=5):
    set_threads(4)
    p = build(True, opts, {})
    S.CameraStop(h); S.CameraSetMediaType(h, 1); S.CameraPlay(h); time.sleep(0.5)
    n = 0; t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        r, hd = S.CameraGetImageBuffer(h, 2000)
        rb = np.frombuffer((ctypes.c_ubyte * hd.uBytes).from_address(r), np.uint8)
        p.unpack(rb); S.CameraReleaseImageBuffer(h, r)          # raw is free as soon as it is unpacked
        p.finish(p.color(p.demosaic(p._bayer))); n += 1
    return round(n / (time.perf_counter() - t0), 1)


def live_stream(workers, omp, opts, seconds=5):
    S.CameraStop(h); S.CameraSetMediaType(h, 1); S.CameraPlay(h); time.sleep(0.5)
    sp = StreamProcessor(W, H, CODE, workers=workers, omp_threads=omp, **opts)
    nslots = workers + 2
    slots = [np.empty(W * H * 3 // 2, np.uint8) for _ in range(nslots)]
    free = queue.Queue()
    for i in range(nslots): free.put(i)
    done = [0]

    def consume(out, tag):
        done[0] += 1
        free.put(tag)
    n = 0; t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        i = free.get()
        r, hd = S.CameraGetImageBuffer(h, 2000)
        ctypes.memmove(slots[i].ctypes.data, r, hd.uBytes)
        S.CameraReleaseImageBuffer(h, r)
        sp.submit(slots[i], consume, tag=i); n += 1
    sp.drain(); dt = time.perf_counter() - t0; sp.close()
    return round(done[0] / dt, 1)


live = {"sdk_8bit_isp": live_sdk8(), "raw_12bit_grab_only(camera limit)": live_raw_only()}
print(live, flush=True)
live["new_pipeline_in_grab_thread(4 threads)"] = live_sync({})
print(live, flush=True)
for opts_name, opts in (("plain", {}), ("everything", CONFIGS[-1][1])):
    for workers, omp in ((1, 4), (2, 2), (3, 1), (4, 1), (2, 1)):
        k = f"stream[{opts_name}] workers={workers} omp={omp}"
        live[k] = live_stream(workers, omp, opts)
        print(k, live[k], flush=True)
res["C_live_fps"] = live
S.CameraStop(h); S.CameraUnInit(h)
print(json.dumps(res, indent=1))
