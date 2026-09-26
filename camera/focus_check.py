"""ChArUco-based focus measurement.

The printed ChArUco board (20x14 squares, DICT_4X4_250) is full of ideal
step edges. After locating the board we measure the edge blur around every
detected checker corner, which gives a contrast-independent sharpness number
plus a per-region map across the field of view (useful to spot lens tilt or
field curvature).

Edge width metric
-----------------
For a blurred step edge of amplitude A, the peak intensity gradient is G.
``A / G`` is the edge width in pixels (with central differences an ideal 1-px
step gives 2.0). Lower is sharper. It does not depend on lighting or print
contrast, so it stays comparable between sessions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

BOARD_COLS = 20
BOARD_ROWS = 14
_CORNERS_PER_ROW = BOARD_COLS - 1

# Detection runs on a downscaled copy (cheap on a Pi); sharpness is measured on
# the full-resolution grayscale so it reflects real sensor pixels.
DETECT_MAX_WIDTH = 1280
MIN_CORNERS = 12
# Below this checker size (px) the patches hold too few pixels to measure blur.
MIN_SQUARE_PX = 8.0
ZONES = 3  # ZONES x ZONES sharpness map

_detector = None


def _get_detector():
    global _detector
    if _detector is None:
        import cv2
        aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
        board = cv2.aruco.CharucoBoard((BOARD_COLS, BOARD_ROWS), 10.0, 8.0, aruco_dict)
        _detector = cv2.aruco.CharucoDetector(board)
    return _detector


@dataclass
class FocusMeasurement:
    detected: bool = False
    reason: str = ""              # why detection/measurement failed
    corners: int = 0
    square_px: float = 0.0        # median checker size in full-res pixels
    edge_px: float | None = None  # median edge width, lower = sharper
    zones: list[list[float | None]] = field(default_factory=list)  # [row][col] edge_px
    tracked: bool = False         # board not re-detected; last known position reused
    # For overlay drawing (full-res coords): x, y, edge_px per corner.
    points: np.ndarray | None = None

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "reason": self.reason,
            "corners": self.corners,
            "tracked": self.tracked,
            "square_px": round(self.square_px, 1),
            "edge_px": None if self.edge_px is None else round(self.edge_px, 3),
            "zones": [
                [None if v is None else round(v, 3) for v in row] for row in self.zones
            ],
        }


def to_gray(frame: np.ndarray) -> np.ndarray:
    """SDK frame (BGR or mono) -> uint8 grayscale."""
    if frame.ndim == 3 and frame.shape[2] == 3:
        import cv2
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame[:, :, 0] if frame.ndim == 3 else frame


def _edge_width(patch: np.ndarray) -> float | None:
    """Edge width (px) of one patch, or None if it has no usable contrast."""
    p = patch.astype(np.float32)
    amp = float(np.percentile(p, 95) - np.percentile(p, 5))
    if amp < 40.0:
        return None
    gy, gx = np.gradient(p)
    g = float(np.percentile(np.hypot(gx, gy), 99.5))
    if g < 1e-3:
        return None
    return amp / g


def _detect(gray: np.ndarray):
    """Return (pts Nx2 full-res, ids, square_px) or (None, n_seen, reason)."""
    import cv2

    h, w = gray.shape[:2]
    scale = 1.0
    small = gray
    if w > DETECT_MAX_WIDTH:
        scale = DETECT_MAX_WIDTH / w
        small = cv2.resize(gray, (DETECT_MAX_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)

    try:
        corners, ids, _mc, _mi = _get_detector().detectBoard(small)
    except Exception as exc:  # cv2 raises on odd inputs; never kill the stream
        return None, 0, f"detector error: {exc}"

    if corners is None or ids is None or len(ids) < MIN_CORNERS:
        n = 0 if ids is None else len(ids)
        return None, n, "Board not found" if n == 0 else f"Only {n} corners visible"

    pts = corners.reshape(-1, 2) / scale
    ids = ids.reshape(-1)

    # Checker size from horizontally adjacent corners (id, id+1 in same row).
    by_id = {int(i): p for i, p in zip(ids, pts)}
    steps = [
        float(np.hypot(*(p - by_id[i + 1])))
        for i, p in by_id.items()
        if i + 1 in by_id and i // _CORNERS_PER_ROW == (i + 1) // _CORNERS_PER_ROW
    ]
    return pts, len(ids), float(np.median(steps)) if steps else 0.0


def _measure_at(gray: np.ndarray, pts: np.ndarray, square_px: float, n_seen: int) -> FocusMeasurement:
    h, w = gray.shape[:2]
    if square_px < MIN_SQUARE_PX:
        return FocusMeasurement(
            detected=True, corners=n_seen, square_px=square_px,
            reason=f"Board too small ({square_px:.0f} px/square) — move closer or zoom in",
        )

    half = max(4, int(round(square_px * 0.35)))
    widths = np.full(len(pts), np.nan, dtype=np.float32)
    for k, (x, y) in enumerate(pts):
        xi, yi = int(round(x)), int(round(y))
        if xi - half < 0 or yi - half < 0 or xi + half >= w or yi + half >= h:
            continue
        ew = _edge_width(gray[yi - half:yi + half + 1, xi - half:xi + half + 1])
        if ew is not None:
            widths[k] = ew

    ok = ~np.isnan(widths)
    if ok.sum() < MIN_CORNERS:
        return FocusMeasurement(
            detected=True, corners=n_seen, square_px=square_px,
            reason="Not enough contrast on the board",
        )

    zones: list[list[float | None]] = []
    for r in range(ZONES):
        row: list[float | None] = []
        for c in range(ZONES):
            sel = (
                ok
                & (pts[:, 0] * ZONES // w == c)
                & (pts[:, 1] * ZONES // h == r)
            )
            row.append(float(np.median(widths[sel])) if sel.sum() >= 3 else None)
        zones.append(row)

    return FocusMeasurement(
        detected=True,
        corners=int(ok.sum()),
        square_px=square_px,
        edge_px=float(np.median(widths[ok])),
        zones=zones,
        points=np.column_stack([pts[ok], widths[ok]]),
    )


class FocusTracker:
    """Per-camera measurement state.

    While turning the focus ring the board can blur past the point where the
    ArUco markers are readable. The board itself does not move, so when
    detection drops out we keep measuring at the last detected corner
    positions and flag the result as ``tracked``.
    """

    def __init__(self) -> None:
        self._pts: np.ndarray | None = None
        self._square_px = 0.0
        self._shape: tuple[int, int] | None = None

    def reset(self) -> None:
        self._pts = None
        self._shape = None

    def measure(self, frame: np.ndarray) -> FocusMeasurement:
        gray = to_gray(frame)
        pts, n_or_none, extra = _detect(gray)

        if pts is not None:
            self._pts, self._square_px, self._shape = pts, extra, gray.shape[:2]
            return _measure_at(gray, pts, extra, n_or_none)

        if self._pts is not None and self._shape == gray.shape[:2]:
            m = _measure_at(gray, self._pts, self._square_px, len(self._pts))
            if m.edge_px is not None:
                m.tracked = True
                return m

        return FocusMeasurement(corners=n_or_none, reason=extra)
