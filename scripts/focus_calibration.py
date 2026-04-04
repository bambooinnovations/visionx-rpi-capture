#!/usr/bin/env python3
"""Focus calibration script for Arducam Hawkeye (IMX519).

Sweeps through a range of LensPosition values and captures one image per step,
saving everything into a single output folder so you can visually pick the
sharpest result and set it as `lens_position` in configuration.toml.

Usage:
    python scripts/focus_calibration.py

    # Custom range / step:
    python scripts/focus_calibration.py --min 0.0 --max 8.0 --step 0.5

    # Save to a specific folder:
    python scripts/focus_calibration.py --output /tmp/focus_sweep

LensPosition units = dioptres (1 / distance_in_metres):
    0.0  → infinity
    0.5  → ~2 m
    1.0  → ~1 m
    2.0  → ~50 cm
    4.0  → ~25 cm
    8.0  → ~12 cm
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Allow running from the repo root or from inside scripts/
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from picamera2 import Picamera2
except ImportError:
    print("ERROR: picamera2 not found. Run: uv sync --extra rpi")
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep focus positions and capture one image per step.")
    p.add_argument("--min",    type=float, default=0.0,  help="Lowest  LensPosition (default 0.0 = infinity)")
    p.add_argument("--max",    type=float, default=8.0,  help="Highest LensPosition (default 8.0 ≈ 12 cm)")
    p.add_argument("--step",   type=float, default=0.5,  help="Step size (default 0.5)")
    p.add_argument("--output", type=Path,  default=None, help="Output folder (default: ./focus_sweep_<timestamp>)")
    p.add_argument("--settle", type=float, default=1.5,  help="Seconds to wait after moving lens (default 1.5)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Build list of positions
    positions: list[float] = []
    v = args.min
    while v <= args.max + 1e-9:
        positions.append(round(v, 2))
        v += args.step

    if not positions:
        print("ERROR: No positions to sweep — check --min / --max / --step values.")
        sys.exit(1)

    # Output folder
    output_dir: Path = args.output or Path(f"./focus_sweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output folder : {output_dir.resolve()}")
    print(f"Positions     : {positions}")
    print(f"Settle delay  : {args.settle}s per step\n")

    cam = Picamera2()

    # Use still config for full-resolution captures
    still_cfg = cam.create_still_configuration()
    cam.configure(still_cfg)
    cam.start()
    time.sleep(2)  # let AE/AWB stabilise

    # Lock AE/AWB so exposure doesn't vary between shots
    meta = cam.capture_metadata()
    cam.set_controls({
        "AeEnable":     False,
        "AwbEnable":    False,
        "ExposureTime": meta["ExposureTime"],
        "AnalogueGain": meta["AnalogueGain"],
        "ColourGains":  meta["ColourGains"],
    })

    if "AfMode" not in cam.camera_controls:
        print("WARNING: Camera does not report AfMode control — may not support motorised focus.")

    total = len(positions)
    for i, pos in enumerate(positions, start=1):
        # Switch to manual focus at this position
        if "AfMode" in cam.camera_controls:
            cam.set_controls({"AfMode": 0, "LensPosition": pos})

        time.sleep(args.settle)  # let the lens physically settle

        filename = output_dir / f"focus_{pos:.2f}.jpg"
        cam.capture_file(str(filename))

        # Approx real-world distance for display
        dist = f"~{1/pos*100:.0f} cm" if pos > 0 else "infinity"
        print(f"  [{i:>2}/{total}]  LensPosition={pos:.2f}  ({dist})  → {filename.name}")

    cam.stop()

    print(f"\nDone. {total} images saved to: {output_dir.resolve()}")
    print("\nNext step:")
    print("  1. Open the folder and find the sharpest image.")
    print("  2. Note its LensPosition value from the filename.")
    print("  3. Set it in configuration.toml:")
    print("       [camera]")
    print("       lens_position = <value>")


if __name__ == "__main__":
    main()
