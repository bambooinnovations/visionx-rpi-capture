"""Pixels-per-cm measurement using the existing ChArUco calibration board.

Workflow
--------
POST /api/pxcm/measure captures one full-resolution frame from the requested
camera (any camera type — MindVision or Pi), detects the ChArUco board via
the same detector used by lens/stitch calibration, and estimates the
pixel/mm scale by averaging pixel distances between grid-adjacent ChArUco
corners (each pair is exactly `square_mm` apart on the physical board, so the
average is a local, perspective-robust scale estimate).

Board detection code is shared with blueprints/stitch.py rather than
duplicated a third time.
"""
from __future__ import annotations

import base64
import shutil
import statistics
import tempfile
from pathlib import Path

import cv2
import numpy as np
import structlog
from flask import Blueprint, jsonify, request

from blueprints.stitch import (
    _DEFAULT_ARUCO_DICT,
    _DEFAULT_BOARD_COLS,
    _DEFAULT_BOARD_ROWS,
    _DEFAULT_MARKER_MM,
    _DEFAULT_SQUARE_MM,
    _detect_charuco,
    _make_board,
    _to_bgr,
    _to_gray,
)
from camera.mindvision import CameraMode, MindVisionCamera
from tasks import CAPTURE_TMP_DIR

logger = structlog.get_logger()

# Known physical boards for the UI dropdown. Only one board exists today
# (targets/charuco_20x14_10mm_checker_8mm_marker_DICT_4X4_250_40px.png) —
# add more entries here if additional printed boards go into use.
_BOARD_PRESETS = [
    {
        "id": "default",
        "label": "20x14, 10mm checker, 8mm marker (DICT_4X4_250)",
        "board_cols": _DEFAULT_BOARD_COLS,
        "board_rows": _DEFAULT_BOARD_ROWS,
        "square_mm": _DEFAULT_SQUARE_MM,
        "marker_mm": _DEFAULT_MARKER_MM,
        "aruco_dict": _DEFAULT_ARUCO_DICT,
    },
]


def _board_params_from_body(body: dict) -> dict:
    return {
        "cols": int(body.get("board_cols", _DEFAULT_BOARD_COLS)),
        "rows": int(body.get("board_rows", _DEFAULT_BOARD_ROWS)),
        "square_mm": float(body.get("square_mm", _DEFAULT_SQUARE_MM)),
        "marker_mm": float(body.get("marker_mm", _DEFAULT_MARKER_MM)),
        "aruco_dict_name": body.get("aruco_dict", _DEFAULT_ARUCO_DICT),
    }


def _estimate_px_per_mm(
    corners: np.ndarray,
    ids: np.ndarray,
    cols: int,
    square_mm: float,
) -> tuple[float, float, int] | None:
    """Median pixel distance between grid-adjacent ChArUco corners, divided by square_mm.

    ChArUco corner ids are row-major over the (cols-1) x (rows-1) interior
    corner grid: id -> (id // (cols-1), id % (cols-1)). Right/below neighbours
    are exactly `square_mm` apart on the physical board, so each such pair
    gives a local scale sample; the median across all of them is robust to
    perspective foreshortening and the occasional bad corner.
    """
    inner_cols = cols - 1
    if inner_cols < 1:
        return None
    pts = {int(i): corners[k, 0, :] for k, i in enumerate(ids[:, 0])}

    ratios: list[float] = []
    for cid, pt in pts.items():
        col = cid % inner_cols
        right = pts.get(cid + 1) if col + 1 < inner_cols else None
        if right is not None:
            ratios.append(float(np.linalg.norm(right - pt)) / square_mm)
        below = pts.get(cid + inner_cols)
        if below is not None:
            ratios.append(float(np.linalg.norm(below - pt)) / square_mm)

    if len(ratios) < 3:
        return None
    return statistics.median(ratios), statistics.pstdev(ratios), len(ratios)


def create_blueprint(cameras: dict[int, object]) -> Blueprint:
    bp = Blueprint("pxcm", __name__, url_prefix="/api/pxcm")

    @bp.route("/boards", methods=["GET"])
    def boards():
        """Known board presets for the calibration board dropdown."""
        return jsonify(_BOARD_PRESETS)

    @bp.route("/measure", methods=["POST"])
    def measure():
        """Capture a full-resolution frame and compute pixels-per-cm from the ChArUco board.

        JSON body:
          camera_id   int    Required — which camera to capture from.
          board_cols  int    Board columns (default 20).
          board_rows  int    Board rows (default 14).
          square_mm   float  Checker square size mm (default 10.0).
          marker_mm   float  ArUco marker size mm (default 8.0).
          aruco_dict  str    ArUco dictionary name (default "DICT_4X4_250").
        """
        body = request.get_json(silent=True) or {}
        cam_id = int(body.get("camera_id", 0))
        cam = cameras.get(cam_id)
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        if isinstance(cam, MindVisionCamera) and cam.mode == CameraMode.HARDWARE_TRIGGER:
            return jsonify({"error": "Camera is in hardware trigger mode; switch modes to capture"}), 409

        params = _board_params_from_body(body)
        try:
            board, aruco_dict_obj = _make_board(**params)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        CAPTURE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=CAPTURE_TMP_DIR))
        try:
            try:
                image_path, _metrics = cam.capture_image(resolution=None, output_folder=tmp_dir)
            except RuntimeError as e:
                logger.warning("pxcm_capture_no_camera", camera_id=cam_id, reason=str(e))
                return jsonify({"error": "No camera detected"}), 503
            except Exception:
                logger.exception("pxcm_capture_failed", camera_id=cam_id)
                return jsonify({"error": "Capture failed"}), 500

            frame = cv2.imread(str(image_path))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if frame is None:
            return jsonify({"error": "Failed to read captured image"}), 500

        h, w = frame.shape[:2]
        gray = _to_gray(frame)
        corners, ids = _detect_charuco(gray, board, aruco_dict_obj)
        if corners is None or ids is None:
            return jsonify({
                "error": "ChArUco board not detected — ensure it fills a good portion of the frame and is well lit",
            }), 422

        result = _estimate_px_per_mm(corners, ids, params["cols"], params["square_mm"])
        if result is None:
            return jsonify({"error": "Too few adjacent corner pairs detected to estimate scale"}), 422
        px_per_mm, std_px_per_mm, pairs_used = result

        vis = _to_bgr(frame).copy()
        cv2.aruco.drawDetectedCornersCharuco(vis, corners, ids)
        label = f"{px_per_mm * 10:.2f} px/cm  ({len(corners)} corners, {pairs_used} pairs)"
        cv2.putText(vis, label, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 5)
        cv2.putText(vis, label, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 220, 0), 2)
        ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
        preview_b64 = base64.b64encode(buf.tobytes()).decode("ascii") if ok else None

        logger.info(
            "pxcm_measured",
            camera_id=cam_id,
            px_per_cm=round(px_per_mm * 10, 4),
            corners=int(len(corners)),
            pairs_used=pairs_used,
        )

        return jsonify({
            "camera_id": cam_id,
            "px_per_mm": round(px_per_mm, 4),
            "px_per_cm": round(px_per_mm * 10, 4),
            "std_px_per_mm": round(std_px_per_mm, 4),
            "corners_detected": int(len(corners)),
            "pairs_used": pairs_used,
            "image_size": [w, h],
            "board": {
                "cols": params["cols"],
                "rows": params["rows"],
                "square_mm": params["square_mm"],
                "marker_mm": params["marker_mm"],
                "aruco_dict": params["aruco_dict_name"],
            },
            "preview_jpeg_base64": preview_b64,
        })

    return bp
