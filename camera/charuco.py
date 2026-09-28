"""Shared ChArUco board helpers (board construction, frame conversion, detection).

Used by blueprints/pxcm.py. Extracted from the archived stitch blueprint
(archive/stitch/blueprints/stitch.py) so the rest of the app no longer depends on it.
"""
from __future__ import annotations

import cv2
import numpy as np

# ── Defaults ───────────────────────────────────────────────────────────────────
DEFAULT_BOARD_COLS = 20
DEFAULT_BOARD_ROWS = 14
DEFAULT_SQUARE_MM = 10.0
DEFAULT_MARKER_MM = 8.0
DEFAULT_ARUCO_DICT = "DICT_4X4_250"

ARUCO_DICT_MAP: dict[str, int] = {
    "DICT_4X4_50": cv2.aruco.DICT_4X4_50,
    "DICT_4X4_100": cv2.aruco.DICT_4X4_100,
    "DICT_4X4_250": cv2.aruco.DICT_4X4_250,
    "DICT_4X4_1000": cv2.aruco.DICT_4X4_1000,
    "DICT_5X5_50": cv2.aruco.DICT_5X5_50,
    "DICT_5X5_100": cv2.aruco.DICT_5X5_100,
    "DICT_5X5_250": cv2.aruco.DICT_5X5_250,
    "DICT_5X5_1000": cv2.aruco.DICT_5X5_1000,
    "DICT_6X6_50": cv2.aruco.DICT_6X6_50,
    "DICT_6X6_100": cv2.aruco.DICT_6X6_100,
    "DICT_6X6_250": cv2.aruco.DICT_6X6_250,
    "DICT_6X6_1000": cv2.aruco.DICT_6X6_1000,
}


def make_board(
    cols: int,
    rows: int,
    square_mm: float,
    marker_mm: float,
    aruco_dict_name: str,
) -> tuple[cv2.aruco.CharucoBoard, cv2.aruco.Dictionary]:
    dict_id = ARUCO_DICT_MAP.get(aruco_dict_name)
    if dict_id is None:
        raise ValueError(f"Unknown aruco dict '{aruco_dict_name}'. Valid: {sorted(ARUCO_DICT_MAP)}")
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_id)
    board = cv2.aruco.CharucoBoard((cols, rows), square_mm, marker_mm, aruco_dict)
    return board, aruco_dict


# ── Frame helpers ──────────────────────────────────────────────────────────────

def to_gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 3 and frame.shape[2] == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame[:, :, 0] if frame.ndim == 3 else frame


def to_bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 1:
        return cv2.cvtColor(frame[:, :, 0], cv2.COLOR_GRAY2BGR)
    return frame


# ── Detection & homography ─────────────────────────────────────────────────────

def detect_charuco(
    gray: np.ndarray,
    board: cv2.aruco.CharucoBoard,
    aruco_dict: cv2.aruco.Dictionary,
) -> tuple[np.ndarray, np.ndarray] | tuple[None, None]:
    detector = cv2.aruco.ArucoDetector(aruco_dict)
    marker_corners, marker_ids, _ = detector.detectMarkers(gray)

    if marker_ids is None or len(marker_ids) < 4:
        return None, None

    _, charuco_corners, charuco_ids = cv2.aruco.interpolateCornersCharuco(
        marker_corners, marker_ids, gray, board
    )

    if charuco_corners is None or charuco_ids is None or len(charuco_corners) < 6:
        return None, None

    return charuco_corners, charuco_ids
