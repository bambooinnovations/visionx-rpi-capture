"""Stitch pipeline benchmark.

Run with:
    uv run python3 tools/benchmark_stitch.py

Simulates the full per-trigger stitch workload using realistic image and canvas
dimensions.  Paste the output for analysis.
"""
from __future__ import annotations

import platform
import subprocess
import sys
import time

import cv2
import numpy as np


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f} ms"


def bench(label: str, fn, n: int = 8) -> float:
    """Return mean wall time in seconds over n runs (first run excluded as warmup)."""
    fn()
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    mean = sum(times) / len(times)
    print(f"  {label:<40} {_ms(mean):>10}")
    return mean


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── System info ────────────────────────────────────────────────────────────────

def print_system_info() -> None:
    print("=" * 60)
    print("  STITCH PIPELINE BENCHMARK")
    print("=" * 60)
    print(f"  Python   : {sys.version.split()[0]}")
    print(f"  Platform : {platform.machine()}  {platform.release()}")
    print(f"  OpenCV   : {cv2.__version__}")
    print(f"  NumPy    : {np.__version__}")

    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Model"):
                    print(f"  Board    : {line.split(':')[1].strip()}")
                    break
    except OSError:
        pass

    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True
        )
        print(f"  Throttle : {result.stdout.strip()}")
    except FileNotFoundError:
        pass

    jpeg_line = [
        l for l in cv2.getBuildInformation().splitlines() if "JPEG" in l and "2000" not in l
    ]
    if jpeg_line:
        print(f"  libjpeg  : {jpeg_line[0].strip()}")


# ── Camera / canvas parameters ─────────────────────────────────────────────────
#
# Adjust these to match your actual hardware if different.
#
CAMERA_H = 2048
CAMERA_W = 2448

# Canvas sizes from the live log (stitch_geometry_rebuilt).
# 2-camera canvas observed: 2796 × 2148.
# 3-camera canvas is estimated; update after first live run with 3 cameras.
CANVAS_2CAM = (2148, 2796)   # (h, w)
CANVAS_3CAM = (2148, 4100)   # (h, w) — estimated; update from log


def make_maps(canvas_h: int, canvas_w: int) -> tuple[np.ndarray, np.ndarray]:
    """Synthetic remap maps: uniform random source coordinates within camera frame."""
    map_x = np.random.uniform(0, CAMERA_W, (canvas_h, canvas_w)).astype(np.float32)
    map_y = np.random.uniform(0, CAMERA_H, (canvas_h, canvas_w)).astype(np.float32)
    return map_x, map_y


def make_mask(canvas_h: int, canvas_w: int) -> np.ndarray:
    """Solid weight mask (worst case — no out-of-bounds pixels zeroed)."""
    return np.ones((canvas_h, canvas_w), dtype=np.float32)


# ── Individual operation benchmarks ────────────────────────────────────────────

def bench_individual(canvas_h: int, canvas_w: int, label: str) -> dict:
    section(f"Individual operations — {label}  ({canvas_w}×{canvas_h})")

    frame_u8 = np.random.randint(0, 255, (CAMERA_H, CAMERA_W, 3), dtype=np.uint8)
    map_x, map_y = make_maps(canvas_h, canvas_w)
    mask = make_mask(canvas_h, canvas_w)
    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
    weight = np.zeros((canvas_h, canvas_w), dtype=np.float32)

    t_remap = bench("cv2.remap (uint8 in, per camera)",
        lambda: cv2.remap(frame_u8, map_x, map_y, cv2.INTER_LINEAR))

    warped_u8 = cv2.remap(frame_u8, map_x, map_y, cv2.INTER_LINEAR)

    t_convert = bench("warped.astype(float32)",
        lambda: warped_u8.astype(np.float32))

    warped_f32 = warped_u8.astype(np.float32)

    t_accum = bench("np.multiply(warped, mask, out=warped); canvas+=",
        lambda: canvas.__iadd__(np.multiply(warped_f32, mask[:, :, np.newaxis], out=warped_f32)))

    t_weight = bench("weight += mask",
        lambda: weight.__iadd__(mask))

    covered = weight > 0
    t_norm = bench("normalise + clip + astype(uint8)",
        lambda: np.clip(canvas / np.where(weight > 0, weight, 1)[:, :, np.newaxis],
                        0, 255).astype(np.uint8))

    result_u8 = np.clip(canvas / np.where(weight > 0, weight, 1)[:, :, np.newaxis],
                        0, 255).astype(np.uint8)

    t_encode = bench("cv2.imencode JPEG q=85",
        lambda: cv2.imencode(".jpg", result_u8, [cv2.IMWRITE_JPEG_QUALITY, 85]))

    _, buf = cv2.imencode(".jpg", result_u8, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"  {'JPEG output size':<40} {len(buf)/1024:>9.0f} KB")

    return dict(
        remap=t_remap,
        convert=t_convert,
        accum=t_accum,
        weight=t_weight,
        norm=t_norm,
        encode=t_encode,
    )


# ── Full stitch simulation ─────────────────────────────────────────────────────

def bench_full_stitch(n_cameras: int, canvas_h: int, canvas_w: int) -> None:
    section(f"Full stitch simulation — {n_cameras} cameras  ({canvas_w}×{canvas_h})")

    frames = [
        np.random.randint(0, 255, (CAMERA_H, CAMERA_W, 3), dtype=np.uint8)
        for _ in range(n_cameras)
    ]
    maps = [make_maps(canvas_h, canvas_w) for _ in range(n_cameras)]
    masks = [make_mask(canvas_h, canvas_w) for _ in range(n_cameras)]

    def full_stitch():
        canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
        weight = np.zeros((canvas_h, canvas_w), dtype=np.float32)
        for i in range(n_cameras):
            warped = cv2.remap(frames[i], maps[i][0], maps[i][1],
                               cv2.INTER_LINEAR).astype(np.float32)
            np.multiply(warped, masks[i][:, :, np.newaxis], out=warped)
            canvas += warped
            weight += masks[i]
        safe_weight = np.where(weight > 0, weight, 1.0)
        result = np.clip(canvas / safe_weight[:, :, np.newaxis], 0, 255).astype(np.uint8)
        cv2.imencode(".jpg", result, [cv2.IMWRITE_JPEG_QUALITY, 85])

    t = bench(f"end-to-end ({n_cameras} cams, no upload)", full_stitch, n=6)

    capture_s = 0.5
    upload_s = 0.05
    total_s = capture_s + t + upload_s

    workers = 4
    throughput = workers / total_s
    trigger_rate = 0.91  # /s from logs (~1.1 s interval)

    print()
    print(f"  {'Estimated breakdown':}")
    print(f"    camera exposure (fixed HW)  : ~500 ms")
    print(f"    stitch compute (this bench)  : {_ms(t)}")
    print(f"    upload (historical)          : ~50 ms")
    print(f"    ─────────────────────────────────────")
    print(f"    total per trigger            : ~{_ms(total_s)}")
    print()
    print(f"  {'Throughput with 4 workers':}")
    print(f"    capacity                     : {throughput:.2f} triggers/s")
    print(f"    incoming trigger rate        : {trigger_rate:.2f} triggers/s")
    margin = (throughput - trigger_rate) / trigger_rate * 100
    status = "OK ✓" if margin > 10 else "TIGHT" if margin > 0 else "OVERLOADED ✗"
    print(f"    headroom                     : {margin:+.0f}%  [{status}]")


# ── Geometry build benchmark ───────────────────────────────────────────────────

def bench_geometry_build(canvas_h: int, canvas_w: int, n_cameras: int) -> None:
    section(f"Geometry build (one-time startup cost) — {n_cameras} cameras")

    n = canvas_w * canvas_h
    yy, xx = np.mgrid[0:canvas_h, 0:canvas_w].astype(np.float64)

    def build():
        canvas_pts = np.stack([xx.ravel(), yy.ravel(), np.ones(n)], axis=0)
        H_inv = np.eye(3)  # identity stands in for real homography
        cam_h = H_inv @ canvas_pts
        cam_h /= cam_h[2:3, :]
        x_cam = cam_h[0]
        y_cam = cam_h[1]
        # Simulate forward distortion model
        x_n = (x_cam - canvas_w / 2) / 1000.0
        y_n = (y_cam - canvas_h / 2) / 1000.0
        r2 = x_n * x_n + y_n * y_n
        radial = 1.0 + 0.1 * r2 + 0.01 * r2 * r2
        x_d = x_n * radial
        y_d = y_n * radial
        _ = x_d.reshape(canvas_h, canvas_w).astype(np.float32)
        _ = y_d.reshape(canvas_h, canvas_w).astype(np.float32)

    t = bench(f"build maps for 1 camera  ({canvas_w}×{canvas_h})", build, n=3)
    print(f"  {'Total build (all cameras, 1 thread)':40} {_ms(t * n_cameras):>10}")
    print(f"  (cached after first trigger — never repeated)")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print_system_info()

    # 2-camera setup (current dev/test)
    ops_2 = bench_individual(*CANVAS_2CAM, "2 cameras")
    bench_full_stitch(2, *CANVAS_2CAM)
    bench_geometry_build(*CANVAS_2CAM, n_cameras=2)

    # 3-camera setup (production)
    ops_3 = bench_individual(*CANVAS_3CAM, "3 cameras (estimated canvas)")
    bench_full_stitch(3, *CANVAS_3CAM)
    bench_geometry_build(*CANVAS_3CAM, n_cameras=3)

    section("Summary")
    for label, ops in [("2-cam", ops_2), ("3-cam (est)", ops_3)]:
        canvas = CANVAS_2CAM if "2" in label else CANVAS_3CAM
        remap_total = ops["remap"] * (2 if "2" in label else 3)
        other = ops["convert"] + ops["accum"] + ops["weight"] + ops["norm"] + ops["encode"]
        other_total = other * (2 if "2" in label else 3) + ops["norm"] + ops["encode"]
        print(f"\n  {label}:")
        print(f"    remap total              : {_ms(remap_total)}")
        print(f"    other ops total          : {_ms(other_total)}")
        print(f"    stitch compute estimate  : {_ms(remap_total + other_total)}")

    print("\n" + "=" * 60)
