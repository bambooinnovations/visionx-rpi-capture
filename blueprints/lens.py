"""Per-camera lens distortion calibration using a ChArUco board.

Workflow
--------
1. Show the board in 15–20 different positions/angles/distances and call
   POST /collect once per position.  The endpoint grabs one frame, detects
   ChArUco corners, and accumulates them in a buffer file.

2. When enough frames are buffered, call POST /compute.  This runs
   cv2.aruco.calibrateCameraCharuco to fit the camera matrix K and
   distortion coefficients D, then stores the result.

3. Any pipeline that needs undistorted frames (e.g. stitching) reads K/D
   via get_camera_intrinsics(cam_id).

Default board: 20 squares (cols) x 14 squares (rows), 10 mm checker, 8 mm
marker, DICT_4X4_250.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import structlog
from flask import Blueprint, jsonify, request

from camera.mindvision import MindVisionCamera

logger = structlog.get_logger()

_CALIBRATION_PATH = Path("data/lens_calibration.json")
_BUFFER_PATH = Path("data/lens_calibration_buffer.json")

_DEFAULT_BOARD_COLS = 20
_DEFAULT_BOARD_ROWS = 14
_DEFAULT_SQUARE_MM = 10.0
_DEFAULT_MARKER_MM = 8.0
_DEFAULT_ARUCO_DICT = "DICT_4X4_250"

# How many frames are needed before compute is allowed / considered complete.
FRAMES_MIN = 10     # compute button unlocks
FRAMES_TARGET = 15  # progress bar turns green / "ready" state

_ARUCO_DICT_MAP: dict[str, int] = {
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


# ── Persistence ────────────────────────────────────────────────────────────────

def _load_calibration() -> dict:
    try:
        with open(_CALIBRATION_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_calibration(data: dict) -> None:
    _CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CALIBRATION_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _load_buffer() -> dict:
    """Buffer format: {str(cam_id): [{"corners": [...], "ids": [...], "img_size": [w, h]}, ...]}"""
    try:
        with open(_BUFFER_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_buffer(data: dict) -> None:
    _BUFFER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_BUFFER_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── Public API used by other modules ──────────────────────────────────────────

def get_camera_intrinsics(cam_id: int) -> tuple[np.ndarray, np.ndarray] | None:
    """Return (K, D) for cam_id if lens calibration is stored, else None."""
    entry = _load_calibration().get(str(cam_id))
    if entry is None:
        return None
    K = np.array(entry["camera_matrix"], dtype=np.float64)
    D = np.array(entry["dist_coeffs"], dtype=np.float64)
    return K, D


# ── Board helpers ──────────────────────────────────────────────────────────────

def _make_board(
    cols: int,
    rows: int,
    square_mm: float,
    marker_mm: float,
    aruco_dict_name: str,
) -> tuple[cv2.aruco.CharucoBoard, cv2.aruco.Dictionary]:
    dict_id = _ARUCO_DICT_MAP.get(aruco_dict_name)
    if dict_id is None:
        raise ValueError(f"Unknown aruco dict '{aruco_dict_name}'. Valid: {sorted(_ARUCO_DICT_MAP)}")
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_id)
    board = cv2.aruco.CharucoBoard((cols, rows), square_mm, marker_mm, aruco_dict)
    return board, aruco_dict


def _board_params_from_body(body: dict) -> dict:
    return {
        "cols": int(body.get("board_cols", _DEFAULT_BOARD_COLS)),
        "rows": int(body.get("board_rows", _DEFAULT_BOARD_ROWS)),
        "square_mm": float(body.get("square_mm", _DEFAULT_SQUARE_MM)),
        "marker_mm": float(body.get("marker_mm", _DEFAULT_MARKER_MM)),
        "aruco_dict_name": body.get("aruco_dict", _DEFAULT_ARUCO_DICT),
    }


def _to_gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 3 and frame.shape[2] == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame[:, :, 0] if frame.ndim == 3 else frame


def _detect_charuco(
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


# ── Blueprint ──────────────────────────────────────────────────────────────────

def create_blueprint(cameras: dict[int, MindVisionCamera]) -> Blueprint:
    import threading
    bp = Blueprint("lens", __name__, url_prefix="/api/lens")

    def _grab_frame(cam_id: int) -> np.ndarray | None:
        cam = cameras.get(cam_id)
        if cam is None:
            return None
        try:
            with cam._lock:
                if not cam._streaming and cam.mode != cam.mode.HARDWARE_TRIGGER:
                    import mvsdk
                    mvsdk.CameraSoftTrigger(cam._h_camera)
                return cam._grab_frame()
        except Exception:
            return None

    @bp.route("", methods=["GET"])
    def status():
        """Return lens calibration status per camera."""
        cal = _load_calibration()
        buf = _load_buffer()
        result = {}
        for cid in sorted(cameras.keys()):
            entry = cal.get(str(cid))
            result[cid] = {
                "calibrated": entry is not None,
                "rms": entry.get("rms") if entry else None,
                "frames_used": entry.get("frames_used") if entry else None,
                "calibrated_at": entry.get("calibrated_at") if entry else None,
                "buffered_frames": len(buf.get(str(cid), [])),
                "frames_min": FRAMES_MIN,
                "frames_target": FRAMES_TARGET,
            }
        return jsonify(result)

    @bp.route("/collect", methods=["POST"])
    def collect():
        """Grab one frame, detect ChArUco corners, and add to the calibration buffer.

        Call this from 15–20 different board positions before calling POST /compute.

        JSON body (all fields optional):
          camera_id   int    Camera index (default 0).
          board_cols  int    Board columns (default 20).
          board_rows  int    Board rows (default 14).
          square_mm   float  Checker square size mm (default 10.0).
          marker_mm   float  ArUco marker size mm (default 8.0).
          aruco_dict  str    ArUco dict name (default "DICT_4X4_250").
        """
        body = request.get_json(silent=True) or {}
        cam_id = int(body.get("camera_id", 0))
        if cam_id not in cameras:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        params = _board_params_from_body(body)
        try:
            board, aruco_dict_obj = _make_board(**params)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        frame = _grab_frame(cam_id)
        if frame is None:
            return jsonify({"error": "Failed to grab frame"}), 503

        h, w = frame.shape[:2]
        corners, ids = _detect_charuco(_to_gray(frame), board, aruco_dict_obj)

        n = int(len(corners)) if corners is not None else 0
        if corners is None or ids is None or n < 6:
            return jsonify({
                "accepted": False,
                "reason": "Too few ChArUco corners detected — reposition the board and try again",
                "corners_detected": n,
            }), 422

        buf = _load_buffer()
        cam_buf = buf.get(str(cam_id), [])
        cam_buf.append({
            "corners": corners[:, 0, :].tolist(),
            "ids": ids[:, 0].tolist(),
            "img_size": [w, h],
        })
        buf[str(cam_id)] = cam_buf
        _save_buffer(buf)

        total = len(cam_buf)
        return jsonify({
            "accepted": True,
            "corners_detected": n,
            "buffered_frames": total,
            "hint": (
                f"Need {max(0, FRAMES_TARGET - total)} more frames — keep collecting"
                if total < FRAMES_TARGET
                else "Ready — call POST /rpi/mindvision/lens/compute"
            ),
        })

    @bp.route("/compute", methods=["POST"])
    def compute():
        """Run lens distortion calibration from the accumulated buffer.

        Requires at least 10 buffered frames. Stores K and D for the camera.
        After this succeeds, re-run POST /rpi/mindvision/stitch/calibrate so
        homographies are refitted to undistorted images.

        JSON body (all fields optional):
          camera_id  int  Camera index (default 0).
        """
        body = request.get_json(silent=True) or {}
        cam_id = int(body.get("camera_id", 0))
        if cam_id not in cameras:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        buf = _load_buffer()
        cam_buf = buf.get(str(cam_id), [])
        if len(cam_buf) < FRAMES_MIN:
            return jsonify({
                "error": f"Only {len(cam_buf)} frames buffered — need at least {FRAMES_MIN}",
                "hint": "Call POST /rpi/mindvision/lens/collect from more board positions",
            }), 422

        img_size = tuple(cam_buf[0]["img_size"])  # (w, h)

        try:
            board, _ = _make_board(
                _DEFAULT_BOARD_COLS, _DEFAULT_BOARD_ROWS,
                _DEFAULT_SQUARE_MM, _DEFAULT_MARKER_MM, _DEFAULT_ARUCO_DICT,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 500

        all_corners = [
            np.array(e["corners"], dtype=np.float32).reshape(-1, 1, 2)
            for e in cam_buf
        ]
        all_ids = [
            np.array(e["ids"], dtype=np.int32).reshape(-1, 1)
            for e in cam_buf
        ]

        try:
            rms, K, D, _, _ = cv2.aruco.calibrateCameraCharuco(
                all_corners, all_ids, board, img_size, None, None
            )
        except cv2.error as e:
            return jsonify({"error": f"Calibration failed: {e}"}), 500

        cal = _load_calibration()
        cal[str(cam_id)] = {
            "camera_matrix": K.tolist(),
            "dist_coeffs": D.tolist(),
            "rms": float(rms),
            "frames_used": len(cam_buf),
            "image_size": list(img_size),
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_calibration(cal)

        buf.pop(str(cam_id), None)
        _save_buffer(buf)

        logger.info("lens_calibrated", camera_id=cam_id, rms=rms, frames=len(cam_buf))
        return jsonify({
            "camera_id": cam_id,
            "rms": float(rms),
            "frames_used": len(cam_buf),
            "hint": "Re-run POST /rpi/mindvision/stitch/calibrate to refit homographies on undistorted images",
        })

    @bp.route("/last", methods=["DELETE"])
    def remove_last():
        """Remove the most recently collected frame from the buffer.

        JSON body (all fields optional):
          camera_id  int  Camera index (default 0).
        """
        body = request.get_json(silent=True) or {}
        cam_id = int(body.get("camera_id", 0))
        if cam_id not in cameras:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        buf = _load_buffer()
        cam_buf = buf.get(str(cam_id), [])
        if not cam_buf:
            return jsonify({"error": "No frames in buffer to remove"}), 422

        cam_buf.pop()
        buf[str(cam_id)] = cam_buf
        _save_buffer(buf)

        total = len(cam_buf)
        return jsonify({
            "removed": True,
            "buffered_frames": total,
        })

    @bp.route("", methods=["DELETE"])
    def clear():
        """Delete stored lens calibration and/or the collection buffer.

        JSON body (all fields optional):
          camera_id  int   Delete only this camera's data (omit to clear all).
          buffer     bool  Also clear the collection buffer (default true).
        """
        body = request.get_json(silent=True) or {}
        cam_id = body.get("camera_id")
        clear_buf = bool(body.get("buffer", True))

        cal = _load_calibration()
        buf = _load_buffer()

        if cam_id is not None:
            key = str(int(cam_id))
            cal.pop(key, None)
            if clear_buf:
                buf.pop(key, None)
            cleared = [int(cam_id)]
        else:
            cal = {}
            if clear_buf:
                buf = {}
            cleared = "all"

        _save_calibration(cal)
        _save_buffer(buf)
        return jsonify({"cleared": cleared})

    return bp
