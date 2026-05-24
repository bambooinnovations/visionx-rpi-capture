#!/usr/bin/env python3
"""Generate a printable Siemens star focus calibration target.

Outputs a high-resolution PNG sized for letter paper (8.5" x 11" at 300 DPI).
Print at 100% scale — no "fit to page" — on a laser or inkjet printer.

Usage:
    .venv/bin/python scripts/gen_siemens_star.py
    .venv/bin/python scripts/gen_siemens_star.py --output /tmp/star.png --dpi 150
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a Siemens star focus target for letter paper.")
    p.add_argument("--output", type=Path, default=Path("siemens_star_letter.png"),
                   help="Output PNG path (default: siemens_star_letter.png)")
    p.add_argument("--dpi", type=int, default=300,
                   help="Output resolution in DPI (default: 300)")
    p.add_argument("--spokes", type=int, default=36,
                   help="Number of black/white spoke pairs — 36 gives 72 sectors (default: 36)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("ERROR: numpy and Pillow are required. Run: uv sync")
        sys.exit(1)

    dpi = args.dpi
    n_pairs = args.spokes

    w = int(8.5 * dpi)
    h = int(11.0 * dpi)
    cx, cy = w // 2, h // 2

    star_r = int(3.6 * dpi)
    center_blank_r = int(0.18 * dpi)
    center_dot_r = int(0.03 * dpi)

    pixels = np.full((h, w, 3), 255, dtype=np.uint8)

    y_idx, x_idx = np.mgrid[0:h, 0:w]
    dx = (x_idx - cx).astype(np.float32)
    dy = (y_idx - cy).astype(np.float32)
    dist = np.sqrt(dx * dx + dy * dy)

    angle = (np.arctan2(dy, dx) + 2 * math.pi) % (2 * math.pi)
    sector = (angle / (2 * math.pi) * (2 * n_pairs)).astype(np.int32)

    black_mask = (sector % 2 == 0) & (dist <= star_r) & (dist > center_blank_r)
    pixels[black_mask] = [0, 0, 0]

    for r in (star_r, star_r + 6):
        ring = (dist >= r) & (dist <= r + 3)
        pixels[ring] = [0, 0, 0]

    pixels[dist <= center_blank_r] = [255, 255, 255]
    pixels[dist <= center_dot_r] = [0, 0, 0]

    img = Image.fromarray(pixels)
    draw = ImageDraw.Draw(img)

    line_len = star_r + 40
    lw = 3
    draw.line([(cx - line_len, cy), (cx + line_len, cy)],
              fill=(0, 0, 0), width=lw)
    draw.line([(cx, cy - line_len), (cx, cy + line_len)],
              fill=(0, 0, 0), width=lw)
    diag = int(line_len * 0.707)
    draw.line([(cx - diag, cy - diag), (cx + diag, cy + diag)],
              fill=(180, 180, 180), width=2)
    draw.line([(cx - diag, cy + diag), (cx + diag, cy - diag)],
              fill=(180, 180, 180), width=2)

    mark = 60
    margin = 80
    for bx, by in [(margin, margin), (w - margin, margin), (margin, h - margin), (w - margin, h - margin)]:
        draw.line([(bx - mark, by), (bx + mark, by)], fill=(0, 0, 0), width=3)
        draw.line([(bx, by - mark), (bx, by + mark)], fill=(0, 0, 0), width=3)

    font_size = max(16, dpi // 8)
    font_sm_size = max(12, dpi // 12)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        font_sm = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_sm_size)
    except Exception:
        font = ImageFont.load_default()
        font_sm = font

    label = f"Siemens Star Focus Target  —  {n_pairs * 2} sectors  —  {dpi} DPI"
    draw.text((cx, h - margin - 10), label,
              fill=(80, 80, 80), font=font, anchor="mb")
    sub = (
        "Point camera at star centre. "
        "Adjust focus until magenta peaking fills the spokes to the centre disc."
    )
    draw.text((cx, h - margin + font_sm_size + 6), sub,
              fill=(120, 120, 120), font=font_sm, anchor="mb")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(args.output), dpi=(dpi, dpi))
    print(f"Saved: {args.output.resolve()}  ({w}x{h} px at {dpi} DPI)")
    print("Print at 100% scale (no 'fit to page') on letter paper.")


if __name__ == "__main__":
    main()
