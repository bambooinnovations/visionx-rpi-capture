"""Generate a print-ready ChArUco target for the focus check (PNG + PDF at exact physical size).

    python scripts/gen_focus_board.py                 # 10x7, 25 mm squares, A4 landscape -> targets/
    python scripts/gen_focus_board.py --cols 20 --rows 14 --square-mm 10
    python scripts/gen_focus_board.py --paper A3 --cols 14 --rows 10 --square-mm 25

The layout must match a board known to camera/focus_check.py (BOARDS). Print at 100 % / "actual size" (turn off
"fit to page"); the caption under the board says how big a square must measure so you can check with a ruler.
Rendering is 12 px per mm, so squares and the 6 cells of every marker are whole pixels (crisp edges).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont

PX_PER_MM = 12
DPI = PX_PER_MM * 25.4
PAPER_MM = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "LETTER": (279.4, 215.9)}
MARKER_RATIO = 0.8        # must match camera/focus_check.py (square_mm * 0.8)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cols", type=int, default=10)
    ap.add_argument("--rows", type=int, default=7)
    ap.add_argument("--square-mm", type=float, default=25.0)
    ap.add_argument("--paper", default="A4", choices=sorted(PAPER_MM))
    ap.add_argument("--portrait", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "targets"))
    a = ap.parse_args()

    sq_px = round(a.square_mm * PX_PER_MM)
    marker_mm = a.square_mm * MARKER_RATIO
    if round(marker_mm * PX_PER_MM) % 6:
        print(f"note: marker is {marker_mm:g} mm = {marker_mm * PX_PER_MM:g} px, not a multiple of 6 cells; edges may soften")
    board_w, board_h = a.cols * sq_px, a.rows * sq_px
    pw, ph = (round(v * PX_PER_MM) for v in PAPER_MM[a.paper])
    if a.portrait:
        pw, ph = ph, pw
    if board_w > pw or board_h > ph:
        raise SystemExit(f"{a.cols}x{a.rows} at {a.square_mm:g} mm is {a.cols * a.square_mm:g} x "
                         f"{a.rows * a.square_mm:g} mm: too big for {a.paper}")

    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
    board = cv2.aruco.CharucoBoard((a.cols, a.rows), a.square_mm, marker_mm, d)
    img = board.generateImage((board_w, board_h), marginSize=0, borderBits=1)

    page = Image.new("L", (pw, ph), 255)
    # top-aligned (10 mm margin) so the caption and ruler fit underneath
    x0, y0 = (pw - board_w) // 2, max(0, min((ph - board_h) // 2, 10 * PX_PER_MM))
    page.paste(Image.fromarray(img), (x0, y0))

    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=3 * PX_PER_MM)
    caption = (f"ChArUco {a.cols}x{a.rows}  DICT_4X4_250  square {a.square_mm:g} mm  marker {marker_mm:g} mm  "
               f"-  print at 100% (no fit-to-page); board must measure {a.cols * a.square_mm:g} x {a.rows * a.square_mm:g} mm")
    draw.text((pw // 2, min(ph - 4 * PX_PER_MM, y0 + board_h + 5 * PX_PER_MM)), caption, fill=0, font=font, anchor="mt")
    # 100 mm ruler bar: lets you verify print scale without the board
    ry = min(ph - 8 * PX_PER_MM, y0 + board_h + 17 * PX_PER_MM)
    rx = x0
    draw.line([(rx, ry), (rx + 100 * PX_PER_MM, ry)], fill=0, width=3)
    for mm in range(0, 101, 10):
        draw.line([(rx + mm * PX_PER_MM, ry - 2 * PX_PER_MM), (rx + mm * PX_PER_MM, ry + 2 * PX_PER_MM)], fill=0, width=3)
    draw.text((rx + 105 * PX_PER_MM, ry), "100 mm", fill=0, font=font, anchor="lm")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = (f"charuco_{a.cols}x{a.rows}_{a.square_mm:g}mm_checker_{marker_mm:g}mm_marker_DICT_4X4_250_{a.paper}")
    png, pdf = out / f"{stem}.png", out / f"{stem}.pdf"
    page.save(png, dpi=(DPI, DPI))
    page.save(pdf, resolution=DPI)
    print(f"wrote {png}\nwrote {pdf}")


if __name__ == "__main__":
    main()
