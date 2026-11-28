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



@dataclass(frozen=True)
class Board:
    key: str
    cols: int            # squares across
    rows: int            # squares down
    square_mm: float
    label: str

    @property
    def corners_per_row(self) -> int:
        return self.cols - 1

    @property
    def markers(self) -> int:
        return self.cols * self.rows // 2


# Printable targets (all DICT_4X4_250, marker = 0.8 x square). Generate with scripts/gen_focus_board.py.
# The 10x7 board has squares twice as big as 20x14 on the same sheet of paper, so each square is twice as
# many camera pixels: that is what makes it detectable from further away / on a wider field of view.
BOARDS: dict[str, Board] = {
    "10x7": Board("10x7", 10, 7, 25.0, "10 x 7 (25 mm squares, large)"),
    "20x14": Board("20x14", 20, 14, 10.0, "20 x 14 (10 mm squares)"),
}
DEFAULT_BOARD = "20x14"


def get_board(key: str | None) -> Board:
    return BOARDS.get(key or DEFAULT_BOARD, BOARDS[DEFAULT_BOARD])


# Detection runs on a downscaled copy (cheap on a Pi); sharpness is measured on
# the full-resolution grayscale so it reflects real sensor pixels.
DETECT_MAX_WIDTH = 1280
MIN_CORNERS = 12
# Below this checker size (px) the patches hold too few pixels to measure blur.
MIN_SQUARE_PX = 8.0
ZONES = 3  # ZONES x ZONES sharpness map

_detectors: dict[str, object] = {}
_last_markers = 0   # ArUco markers seen by the latest detectBoard call (diagnostics only)


def _get_detector(lay: Board):
    det = _detectors.get(lay.key)
    if det is None:
        import cv2
        aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
        board = cv2.aruco.CharucoBoard((lay.cols, lay.rows), lay.square_mm, lay.square_mm * 0.8, aruco_dict)
        det = _detectors[lay.key] = cv2.aruco.CharucoDetector(board)
    return det


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


def _board(img: np.ndarray, lay: Board):
    """(corners, ids, marker_corners) from the detector, or Nones if it errors; never kill the stream."""
    global _last_markers
    try:
        corners, ids, mcorners, mids = _get_detector(lay).detectBoard(img)
    except Exception:
        return None, None, None
    _last_markers = 0 if mids is None else len(mids)
    # Keep only markers that belong to this board: other ArUco targets in view (colour charts) must not
    # stretch the search region.
    if mids is not None and mcorners:
        keep = [k for k, i in enumerate(mids.reshape(-1)) if int(i) < lay.markers]
        mcorners = [mcorners[k] for k in keep]
    return corners, ids, mcorners


def _enough(ids) -> bool:
    return ids is not None and len(ids) >= MIN_CORNERS


# Image transforms that make a valid board unreadable (ArUco markers are not mirror/invert safe).
_SUSPECTS = (
    # A vertical flip is a mirror plus a 180 degree turn, and turns are fine, so one probe covers both.
    ("mirrored (or flipped)", lambda g: g[:, ::-1]),
    ("colour-inverted", lambda g: 255 - g),
)

# OpenCV only turns detected markers into checker corners when a square is at least ~38 px (verified on a real
# frame: 30 px found every marker but 0 corners, 38 px found 230 of 247). Aim a little above that.
TARGET_SQUARE_PX = 55.0
MAX_UPSCALE = 4.0


def _diagnose(small: np.ndarray, lay: Board) -> str | None:
    for name, fn in _SUSPECTS:
        _c, ids, _m = _board(np.ascontiguousarray(fn(small)), lay)
        if _enough(ids):
            return f"Board found but image looks {name} - check camera mirror/flip/invert settings"
    return None


def _square_from(pts: np.ndarray, ids: np.ndarray, lay: Board) -> float:
    """Median checker size from horizontally adjacent corners (id, id+1 in the same row), in pts' units."""
    by_id = {int(i): p for i, p in zip(ids, pts)}
    steps = [
        float(np.hypot(*(p - by_id[i + 1])))
        for i, p in by_id.items()
        if i + 1 in by_id and i // lay.corners_per_row == (i + 1) // lay.corners_per_row
    ]
    return float(np.median(steps)) if steps else 0.0


def _marker_square_px(mcorners) -> float:
    """Estimated checker size from the markers seen (a marker is ~0.73-0.8 of a square)."""
    if not mcorners:
        return 0.0
    sides = [float(np.linalg.norm(m.reshape(4, 2)[0] - m.reshape(4, 2)[1])) for m in mcorners]
    return float(np.median(sides)) / 0.75


def _board_cluster(mc) -> np.ndarray:
    """Corner points of the markers that sit in a tight grid (the board); isolated ones (colour charts) are dropped."""
    pts = np.concatenate([m.reshape(4, 2) for m in mc]).reshape(-1, 4, 2)
    ctr = pts.mean(axis=1)
    sq = _marker_square_px(mc)
    d = np.hypot(ctr[:, None, 0] - ctr[None, :, 0], ctr[:, None, 1] - ctr[None, :, 1])
    keep = (d < 2.2 * sq).sum(axis=1) - 1 >= 3
    return (pts[keep] if keep.sum() >= 4 else pts).reshape(-1, 2)


def _roi_around(xy: np.ndarray, shape: tuple[int, int], square_px: float) -> tuple[int, int, int, int]:
    h, w = shape
    m = int(square_px * 1.5) + 4
    return (
        max(0, int(xy[:, 0].min()) - m), max(0, int(xy[:, 1].min()) - m),
        min(w, int(xy[:, 0].max()) + m), min(h, int(xy[:, 1].max()) + m),
    )


def _detect_zoomed(gray: np.ndarray, roi: tuple[int, int, int, int], square_px: float, lay: Board):
    """Detect inside roi (full-res x0,y0,x1,y1), enlarged so checkers are big enough. Returns (pts, ids) or None."""
    import cv2
    x0, y0, x1, y1 = roi
    crop = gray[y0:y1, x0:x1]
    z = min(MAX_UPSCALE, max(1.0, TARGET_SQUARE_PX / max(square_px, 1.0)))
    if z > 1.05:
        crop = cv2.resize(crop, None, fx=z, fy=z, interpolation=cv2.INTER_CUBIC)
    corners, ids, _m = _board(crop, lay)
    if not _enough(ids):
        return None
    pts = corners.reshape(-1, 2) / z + np.array([x0, y0], dtype=np.float32)
    return pts, ids.reshape(-1)


def _found(gray: np.ndarray, pts: np.ndarray, ids: np.ndarray, lay: Board):
    sq = _square_from(pts, ids, lay)
    return pts, len(ids), sq, (_roi_around(pts, gray.shape[:2], sq), sq)


def _detect(gray: np.ndarray, lay: Board, diagnose: bool = False, roi_hint=None):
    """Return (pts Nx2 full-res, n, square_px, roi_hint) or (None, n_seen, reason, None)."""
    import cv2

    h, w = gray.shape[:2]

    # Fast path: the board was just found here, so search only that region (enlarged).
    if roi_hint is not None:
        hit = _detect_zoomed(gray, roi_hint[0], roi_hint[1], lay)
        if hit is not None:
            return _found(gray, *hit, lay)

    scale = 1.0
    small = gray
    if w > DETECT_MAX_WIDTH:
        scale = DETECT_MAX_WIDTH / w
        small = cv2.resize(gray, (DETECT_MAX_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)

    corners, ids, mc = _board(small, lay)
    markers = _last_markers
    if not _enough(ids) and scale < 1.0:
        # A board filling little of the frame has markers too small after downscaling: retry at full size.
        c2, i2, m2 = _board(gray, lay)
        markers = max(markers, _last_markers)
        if i2 is not None and (ids is None or len(i2) > len(ids)):
            corners, ids, mc, scale = c2, i2, m2, 1.0
        elif m2 is not None and (mc is None or len(m2) > len(mc)):
            mc, scale = m2, 1.0

    if not _enough(ids) and mc is not None and len(mc) >= 4:
        # Markers are readable but the squares are too small for OpenCV to place checker corners:
        # crop around the markers and enlarge.
        allpts = _board_cluster(mc) / scale
        sq_est = _marker_square_px(mc) / scale
        if sq_est > 0:
            hit = _detect_zoomed(gray, _roi_around(allpts, gray.shape[:2], sq_est), sq_est, lay)
            if hit is not None:
                return _found(gray, *hit, lay)

    if not _enough(ids):
        n = 0 if ids is None else len(ids)
        reason = "Board not found" if n == 0 else f"Only {n} corners visible"
        if diagnose:
            reason = _diagnose(small, lay) or reason
        if reason == "Board not found":
            # 0 markers: image unreadable/too small/out of focus. Some markers but no board corners:
            # the printed board does not match the selected layout (or squares are tiny).
            reason = f"Board not found ({markers} ArUco markers seen)"
        return None, n, reason, None

    return _found(gray, corners.reshape(-1, 2) / scale, ids.reshape(-1), lay)


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

    def __init__(self, board: str | Board | None = None) -> None:
        self.board = board if isinstance(board, Board) else get_board(board)
        self._pts: np.ndarray | None = None
        self._square_px = 0.0
        self._shape: tuple[int, int] | None = None
        self._misses = 0
        self._last_reason = ""
        self._roi = None

    def reset(self) -> None:
        self._pts = None
        self._shape = None
        self._roi = None

    def measure(self, frame: np.ndarray) -> FocusMeasurement:
        gray = to_gray(frame)
        if self._shape is not None and self._shape != gray.shape[:2]:
            self._roi = None
        # The mirror/flip/invert probe costs extra detections: run it every 8th miss only.
        pts, n_or_none, extra, roi = _detect(gray, self.board, diagnose=self._misses % 8 == 0, roi_hint=self._roi)
        self._misses = 0 if pts is not None else self._misses + 1
        if pts is None and self._misses % 8 != 1 and self._last_reason:
            extra = self._last_reason if extra.startswith("Board not found") else extra
        if pts is None:
            self._last_reason = extra
            self._roi = None   # lost it: search the whole frame next time

        if pts is not None:
            self._pts, self._square_px, self._shape = pts, extra, gray.shape[:2]
            self._roi = roi
            return _measure_at(gray, pts, extra, n_or_none)

        if self._pts is not None and self._shape == gray.shape[:2]:
            m = _measure_at(gray, self._pts, self._square_px, len(self._pts))
            if m.edge_px is not None:
                m.tracked = True
                return m

        return FocusMeasurement(corners=n_or_none, reason=extra)
