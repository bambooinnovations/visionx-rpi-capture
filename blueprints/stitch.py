"""ChArUco-based multi-camera stitching calibration and composite view.

Calibration workflow
--------------------
Each camera's homography H_i maps camera-i pixels → ChArUco board world plane
(in pixels, scale derived from camera resolution vs board physical size).

At stitch time the board plane is only used as an intermediate coordinate
system.  The lowest-ID calibrated camera is chosen as the reference frame and
all other cameras are warped into it via:

    H_rel = inv(H_ref) @ H_i   (cam_i pixels → cam_ref pixels)

The output canvas is sized dynamically to encompass every camera's full field
of view, with a translation offset applied so no camera lands at negative
coordinates.  Small physical rotations and perspective misalignment are
corrected automatically because H is a full 8-DOF perspective transform.

Default board: 20 squares (cols) x 14 squares (rows), 10 mm checker, 8 mm
marker, DICT_4X4_250.  A smaller board can be specified per calibration pass
if the overlap zone is narrow.

3-camera workflow (outer cameras don't share FOV):
  1. Place board where cam0 + cam1 can see it:
       POST /calibrate  {"cameras": [0, 1]}
  2. Move board where cam1 + cam2 can see it:
       POST /calibrate  {"cameras": [1, 2]}
  Each step merges into the same calibration file.  The center camera's
  homography anchors all three into one coordinate space.
"""
from __future__ import annotations

import io
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import structlog
from flask import Blueprint, Response, jsonify, request, send_file

from camera.mindvision import CameraMode, MindVisionCamera
from blueprints.lens import get_camera_intrinsics

logger = structlog.get_logger()

# ── Defaults ───────────────────────────────────────────────────────────────────
_DEFAULT_BOARD_COLS = 20
_DEFAULT_BOARD_ROWS = 14
_DEFAULT_SQUARE_MM = 10.0
_DEFAULT_MARKER_MM = 8.0
_DEFAULT_ARUCO_DICT = "DICT_4X4_250"

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

_CALIBRATION_PATH = Path("data/stitch_calibration.json")
_CONFIG_PATH = Path("data/stitch_config.json")

_DEFAULT_CONFIG = {
    "min_corners": 40,
    "camera_order": None,  # null = auto (sorted by ID); list[int] = explicit left→right order
}


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            data = json.load(f)
        return {**_DEFAULT_CONFIG, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_CONFIG)


def _save_config(data: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── Board ──────────────────────────────────────────────────────────────────────

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


def _board_spec_from_body(body: dict, existing: dict | None) -> dict | tuple[None, str]:
    """Resolve board spec from request body, falling back to existing calibration then defaults.

    Returns the spec dict, or (None, error_message) on mismatch.
    """
    if existing and "board" in existing:
        stored = existing["board"]
        spec = {
            "cols": stored["cols"],
            "rows": stored["rows"],
            "square_mm": stored["square_mm"],
            "marker_mm": stored["marker_mm"],
            "aruco_dict": stored["dict"],
        }
        # If caller explicitly specified board params, verify they match.
        mismatches = []
        for key, body_key in [("cols", "board_cols"), ("rows", "board_rows"),
                               ("square_mm", "square_mm"), ("marker_mm", "marker_mm"),
                               ("aruco_dict", "aruco_dict")]:
            if body_key in body and body[body_key] != stored.get(key) and body[body_key] != stored.get("dict"):
                mismatches.append(f"{body_key}: existing={stored.get(key, stored.get('dict'))}, requested={body[body_key]}")
        if mismatches:
            return None, (
                "Board spec mismatch with existing calibration: " + "; ".join(mismatches) +
                ". DELETE /calibrate to start over with a new board."
            )
        return spec
    return {
        "cols": int(body.get("board_cols", _DEFAULT_BOARD_COLS)),
        "rows": int(body.get("board_rows", _DEFAULT_BOARD_ROWS)),
        "square_mm": float(body.get("square_mm", _DEFAULT_SQUARE_MM)),
        "marker_mm": float(body.get("marker_mm", _DEFAULT_MARKER_MM)),
        "aruco_dict": body.get("aruco_dict", _DEFAULT_ARUCO_DICT),
    }


# ── Frame helpers ──────────────────────────────────────────────────────────────

def _to_gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 3 and frame.shape[2] == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame[:, :, 0] if frame.ndim == 3 else frame


def _to_bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 1:
        return cv2.cvtColor(frame[:, :, 0], cv2.COLOR_GRAY2BGR)
    return frame


# ── Detection & homography ─────────────────────────────────────────────────────

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


def _compute_homography(
    corners: np.ndarray,
    ids: np.ndarray,
    board: cv2.aruco.CharucoBoard,
    frame_shape: tuple[int, int],
) -> tuple[np.ndarray, int, tuple[int, int]] | None:
    """Compute H mapping camera pixels → board world plane in pixels.

    The scale is set so that 1 board pixel ≈ 1 camera pixel (derived from
    the camera's resolution and physical board size).

    Returns (H, inlier_count, (canvas_w, canvas_h)) or None if fitting fails.
    """
    obj_pts, img_pts = board.matchImagePoints(corners, ids)

    if obj_pts is None or img_pts is None or len(obj_pts) < 6:
        return None

    cam_h, cam_w = frame_shape

    # Derive px_per_mm from camera resolution and board physical size.
    board_size = board.getChessboardSize()
    board_w_mm = board_size[0] * board.getSquareLength()
    board_h_mm = board_size[1] * board.getSquareLength()
    px_per_mm = min(cam_w / board_w_mm, cam_h / board_h_mm)

    world_2d = (obj_pts[:, 0, :2] * px_per_mm).astype(np.float32)
    image_2d = img_pts[:, 0, :].astype(np.float32)

    H, mask = cv2.findHomography(image_2d, world_2d, cv2.RANSAC, 5.0)
    if H is None or mask is None:
        return None

    inliers = int(mask.sum())
    if inliers < 6:
        return None

    canvas_w = int(board_w_mm * px_per_mm)
    canvas_h = int(board_h_mm * px_per_mm)

    return H, inliers, (canvas_w, canvas_h)


# ── Calibration persistence ────────────────────────────────────────────────────

def _load_calibration() -> dict | None:
    try:
        with open(_CALIBRATION_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_calibration(data: dict) -> None:
    _CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CALIBRATION_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── Stitching ──────────────────────────────────────────────────────────────────

_MAX_CANVAS_PX = 16_000  # guard against degenerate homographies

def _stitch_frames(
    frames: dict[int, np.ndarray],
    calibration: dict,
    camera_order: list[int] | None = None,
    max_width: int | None = None,
) -> np.ndarray | None:
    """Warp all cameras into the reference camera's pixel space and blend.

    The reference camera is camera_order[0] when specified, otherwise the
    lowest-ID calibrated camera.  Each other camera's relative homography is:
        H_rel = inv(H_ref) @ H_i
    which maps cam_i pixels directly into the reference camera pixel space.
    The canvas is sized to encompass every camera's full field of view.

    max_width caps the reference camera's width before warping.  All frames are
    scaled by the same factor s = max_width / ref_w so the homographies remain
    consistent: H_rel_scaled = S @ H_rel @ S_inv where S = diag(s, s, 1).
    """
    cam_data = calibration["cameras"]

    available_set = {cid for cid in frames if str(cid) in cam_data}
    if not available_set:
        return None

    if camera_order:
        # Honour the configured order; append any unconfigured cameras at the end.
        available = [cid for cid in camera_order if cid in available_set]
        available += sorted(cid for cid in available_set if cid not in set(available))
    else:
        available = sorted(available_set)

    if not available:
        return None

    ref_id = available[0]
    H_ref = np.array(cam_data[str(ref_id)]["homography"], dtype=np.float64)
    H_ref_inv = np.linalg.inv(H_ref)  # board world → ref camera pixels

    # Pre-undistort all frames if intrinsics are available.
    undistorted: dict[int, np.ndarray] = {}
    for cam_id in available:
        intr = get_camera_intrinsics(cam_id)
        if intr is not None:
            K, D = intr
            undistorted[cam_id] = cv2.undistort(frames[cam_id], K, D)
        else:
            undistorted[cam_id] = frames[cam_id]

    # Apply per-camera colour correction if calibrated.
    color_correction = calibration.get("color_correction", {})
    if color_correction:
        for cam_id in available:
            cc = color_correction.get(str(cam_id))
            f = undistorted[cam_id]
            if cc and f.ndim == 3 and f.shape[2] == 3:
                f = f.astype(np.float32)
                f[:, :, 0] *= cc["b"]
                f[:, :, 1] *= cc["g"]
                f[:, :, 2] *= cc["r"]
                undistorted[cam_id] = np.clip(f, 0, 255).astype(np.uint8)

    # Downscale all frames uniformly so the warp operates on smaller images.
    # All H_rel are adjusted: H_rel_scaled = S @ H_rel @ S_inv.
    ref_h, ref_w = undistorted[ref_id].shape[:2]
    scale = (max_width / ref_w) if (max_width and max_width > 0 and ref_w > max_width) else 1.0

    if scale < 1.0:
        for cam_id in available:
            h, w = undistorted[cam_id].shape[:2]
            undistorted[cam_id] = cv2.resize(
                undistorted[cam_id],
                (max(1, int(w * scale)), max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ref_h, ref_w = undistorted[ref_id].shape[:2]

    S = np.array([[scale, 0, 0], [0, scale, 0], [0, 0, 1]], dtype=np.float64)
    S_inv = np.diag([1.0 / scale, 1.0 / scale, 1.0]).astype(np.float64)

    # Build per-camera relative homographies (cam_i → ref space) and collect
    # all projected corner points to size the canvas.
    rel_homographies: dict[int, np.ndarray] = {ref_id: np.eye(3, dtype=np.float64)}
    corner_pts: list[np.ndarray] = [
        np.array([[0, 0], [ref_w, 0], [ref_w, ref_h], [0, ref_h]], dtype=np.float32)
    ]

    for cam_id in available:
        if cam_id == ref_id:
            continue
        H_i = np.array(cam_data[str(cam_id)]["homography"], dtype=np.float64)
        H_rel = S @ (H_ref_inv @ H_i) @ S_inv
        rel_homographies[cam_id] = H_rel

        h_i, w_i = undistorted[cam_id].shape[:2]
        corners_i = np.array(
            [[0, 0], [w_i, 0], [w_i, h_i], [0, h_i]], dtype=np.float32
        ).reshape(-1, 1, 2)
        projected = cv2.perspectiveTransform(corners_i, H_rel).reshape(-1, 2)
        corner_pts.append(projected)

    all_pts = np.concatenate(corner_pts, axis=0)
    x_min, y_min = all_pts.min(axis=0)
    x_max, y_max = all_pts.max(axis=0)

    offset_x = float(-min(0.0, x_min))
    offset_y = float(-min(0.0, y_min))

    canvas_w = int(np.ceil(x_max + offset_x))
    canvas_h = int(np.ceil(y_max + offset_y))

    if canvas_w > _MAX_CANVAS_PX or canvas_h > _MAX_CANVAS_PX or canvas_w <= 0 or canvas_h <= 0:
        logger.warning("stitch_canvas_size_rejected", w=canvas_w, h=canvas_h)
        return None

    T = np.array([[1, 0, offset_x], [0, 1, offset_y], [0, 0, 1]], dtype=np.float64)

    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
    weight = np.zeros((canvas_h, canvas_w), dtype=np.float32)

    for cam_id in available:
        H = T @ rel_homographies[cam_id]
        frame = undistorted[cam_id]
        bgr = _to_bgr(frame).astype(np.float32)
        src_mask = np.ones(frame.shape[:2], dtype=np.float32)

        warped = cv2.warpPerspective(bgr, H, (canvas_w, canvas_h),
                                     flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        warped_mask = cv2.warpPerspective(src_mask, H, (canvas_w, canvas_h),
                                          flags=cv2.INTER_LINEAR,
                                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)

        canvas += warped * warped_mask[:, :, np.newaxis]
        weight += warped_mask

    covered = weight > 0
    if not covered.any():
        return None

    canvas[covered] /= weight[covered, np.newaxis]
    return np.clip(canvas, 0, 255).astype(np.uint8)


# ── Blueprint ──────────────────────────────────────────────────────────────────

def create_blueprint(cameras: dict[int, MindVisionCamera]) -> Blueprint:
    bp = Blueprint("stitch", __name__, url_prefix="/api/stitch")

    def _grab_frames(camera_ids: list[int]) -> tuple[dict[int, np.ndarray], dict[int, str]]:
        frames: dict[int, np.ndarray] = {}
        errors: dict[int, str] = {}
        mu = threading.Lock()

        def grab_one(cam_id: int, cam: MindVisionCamera) -> None:
            try:
                with cam._lock:
                    if not cam._streaming and cam.mode != CameraMode.HARDWARE_TRIGGER:
                        import mvsdk
                        mvsdk.CameraSoftTrigger(cam._h_camera)
                    frame, _head = cam._grab_frame()
                if frame is None:
                    with mu:
                        errors[cam_id] = "no frame returned"
                else:
                    with mu:
                        frames[cam_id] = frame
            except Exception as exc:
                with mu:
                    errors[cam_id] = str(exc)

        threads = [
            threading.Thread(target=grab_one, args=(cid, cameras[cid]), daemon=True)
            for cid in camera_ids
            if cid in cameras
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for cid in camera_ids:
            if cid not in cameras:
                errors[cid] = "camera not found"

        return frames, errors

    # ── Config ─────────────────────────────────────────────────────────────────

    @bp.route("/config", methods=["GET"])
    def get_config():
        """Return current runtime configuration."""
        return jsonify(_load_config())

    @bp.route("/config", methods=["POST"])
    def update_config():
        """Update runtime configuration. Changes take effect immediately without restart.

        JSON body (all fields optional):
          min_corners  int  Minimum ChArUco corners required per camera (default 40).
        """
        body = request.get_json(silent=True) or {}
        config = _load_config()

        if "min_corners" in body:
            val = int(body["min_corners"])
            if val < 6:
                return jsonify({"error": "min_corners must be at least 6"}), 400
            config["min_corners"] = val

        if "camera_order" in body:
            val = body["camera_order"]
            if val is not None:
                if not isinstance(val, list) or not all(isinstance(i, int) for i in val):
                    return jsonify({"error": "camera_order must be a list of integer camera IDs or null"}), 400
                if len(val) != len(set(val)):
                    return jsonify({"error": "camera_order must not contain duplicate IDs"}), 400
            config["camera_order"] = val

        unknown = [k for k in body if k not in _DEFAULT_CONFIG]
        if unknown:
            return jsonify({"error": f"Unknown config keys: {unknown}"}), 400

        _save_config(config)
        return jsonify(config)

    # ── Calibrate ──────────────────────────────────────────────────────────────

    @bp.route("/calibrate", methods=["POST"])
    def calibrate():
        """Detect ChArUco on the specified cameras and merge their homographies.

        Always merges into the existing calibration file — only the cameras
        listed in this request are updated; others are preserved.

        JSON body (all fields optional):
          cameras     list[int]  Camera IDs to calibrate (default: all connected).
          board_cols  int        Board columns (default 20).
          board_rows  int        Board rows (default 14).
          square_mm   float      Checker square size in mm (default 10.0).
          marker_mm   float      ArUco marker size in mm (default 8.0).
          aruco_dict  str        ArUco dictionary name (default "DICT_4X4_250").

        Board spec must match any existing calibration. DELETE /calibrate to reset.
        Minimum corners threshold is configurable via POST /config.
        """
        body = request.get_json(silent=True) or {}
        config = _load_config()
        min_corners: int = config["min_corners"]

        requested_ids: list[int] = body.get("cameras") or sorted(cameras.keys())
        if not isinstance(requested_ids, list) or not all(isinstance(i, int) for i in requested_ids):
            return jsonify({"error": "'cameras' must be a list of integer camera IDs"}), 400

        existing = _load_calibration() or {}
        spec_or_err = _board_spec_from_body(body, existing)
        if isinstance(spec_or_err, tuple):
            return jsonify({"error": spec_or_err[1]}), 400
        spec = spec_or_err

        try:
            board, aruco_dict = _make_board(
                spec["cols"], spec["rows"], spec["square_mm"],
                spec["marker_mm"], spec["aruco_dict"],
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        frames, grab_errors = _grab_frames(requested_ids)
        if not frames:
            return jsonify({
                "error": "No frames captured",
                "details": {str(k): v for k, v in grab_errors.items()},
            }), 503

        new_results: dict[str, dict] = {}
        failed: dict[str, str] = {}

        for cam_id, frame in frames.items():
            intr = get_camera_intrinsics(cam_id)
            if intr is not None:
                K, D = intr
                frame = cv2.undistort(frame, K, D)

            gray = _to_gray(frame)
            corners, ids = _detect_charuco(gray, board, aruco_dict)

            if corners is None or ids is None:
                failed[str(cam_id)] = (
                    "ChArUco not detected — ensure the board fills a significant "
                    "portion of the frame and is well lit"
                )
                continue

            if len(corners) < min_corners:
                failed[str(cam_id)] = (
                    f"only {len(corners)} corners detected (min {min_corners}) — "
                    "move the board closer, improve lighting, or use a smaller board"
                )
                continue

            result = _compute_homography(corners, ids, board, frame.shape[:2])
            if result is None:
                failed[str(cam_id)] = "homography fit failed (collinear points or insufficient RANSAC inliers)"
                continue

            H, inliers, (canvas_w, canvas_h) = result
            new_results[str(cam_id)] = {
                "homography": H.tolist(),
                "corners_detected": int(len(corners)),
                "inliers": inliers,
                "canvas_width_px": canvas_w,
                "canvas_height_px": canvas_h,
                "calibrated_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.info("stitch_camera_calibrated", camera_id=cam_id, corners=len(corners), inliers=inliers)

        if not new_results:
            return jsonify({
                "error": "No camera in this pass produced a valid homography",
                "details": failed,
                "grab_errors": {str(k): v for k, v in grab_errors.items()},
            }), 422

        # Canvas is the max across all calibrated cameras (they share a world plane).
        merged_cameras = dict(existing.get("cameras", {}))
        merged_cameras.update(new_results)

        canvas_w = max(v["canvas_width_px"] for v in merged_cameras.values())
        canvas_h = max(v["canvas_height_px"] for v in merged_cameras.values())

        calibration = {
            "board": {
                "cols": spec["cols"],
                "rows": spec["rows"],
                "square_mm": spec["square_mm"],
                "marker_mm": spec["marker_mm"],
                "dict": spec["aruco_dict"],
            },
            "canvas_width_px": canvas_w,
            "canvas_height_px": canvas_h,
            "cameras": merged_cameras,
        }
        _save_calibration(calibration)

        all_calibrated = sorted(int(k) for k in merged_cameras)
        logger.info(
            "stitch_calibration_updated",
            updated=sorted(int(k) for k in new_results),
            total_calibrated=all_calibrated,
        )

        return jsonify({
            "updated": sorted(int(k) for k in new_results),
            "failed": failed,
            "grab_errors": {str(k): v for k, v in grab_errors.items()},
            "all_calibrated": all_calibrated,
            "canvas": {"width": canvas_w, "height": canvas_h},
        })

    @bp.route("/calibrate", methods=["GET"])
    def calibration_status():
        """Return calibration metadata, or a clear status when not calibrated."""
        cal = _load_calibration()
        all_camera_ids = sorted(cameras.keys())

        if cal is None:
            return jsonify({
                "calibrated": False,
                "cameras_calibrated": [],
                "cameras_missing": all_camera_ids,
                "hint": "POST /rpi/mindvision/stitch/calibrate with {\"cameras\": [id1, id2]}",
            }), 404

        calibrated_ids = sorted(int(k) for k in cal.get("cameras", {}))
        missing_ids = [cid for cid in all_camera_ids if cid not in calibrated_ids]

        return jsonify({
            "calibrated": True,
            "cameras_calibrated": calibrated_ids,
            "cameras_missing": missing_ids,
            "ready_to_stitch": len(missing_ids) == 0,
            "board": cal.get("board"),
            "canvas": {
                "width": cal.get("canvas_width_px"),
                "height": cal.get("canvas_height_px"),
            },
            "cameras": {
                cam_id: {
                    "corners_detected": v["corners_detected"],
                    "inliers": v.get("inliers"),
                    "calibrated_at": v.get("calibrated_at"),
                }
                for cam_id, v in cal.get("cameras", {}).items()
            },
            "color_correction": {
                "calibrated": "color_correction" in cal,
                "calibrated_at": cal.get("color_correction_calibrated_at"),
            },
        })

    @bp.route("/calibrate", methods=["DELETE"])
    def clear_calibration():
        """Delete the saved calibration file and start fresh."""
        try:
            _CALIBRATION_PATH.unlink()
            logger.info("stitch_calibration_cleared")
            return jsonify({"cleared": True})
        except FileNotFoundError:
            return jsonify({"error": "No calibration file found"}), 404

    # ── Colour calibration ─────────────────────────────────────────────────────

    @bp.route("/calibrate-color", methods=["GET"])
    def color_calibration_status():
        """Return current per-camera colour correction status."""
        cal = _load_calibration()
        if cal is None or "color_correction" not in cal:
            return jsonify({"calibrated": False})
        return jsonify({
            "calibrated": True,
            "calibrated_at": cal.get("color_correction_calibrated_at"),
            "reference_camera": min(int(k) for k in cal["color_correction"]),
            "corrections": cal["color_correction"],
        })

    @bp.route("/calibrate-color", methods=["POST"])
    def calibrate_color():
        """Capture frames from all cameras pointed at a neutral reference and compute
        per-camera BGR correction multipliers relative to the lowest-ID camera.

        Requires stitch calibration to already be complete.
        Point all cameras at the same white/grey surface before calling this.
        """
        cal, err_resp = _calibration_preflight()
        if err_resp is not None:
            return err_resp

        cam_ids = sorted(int(k) for k in cal["cameras"])
        frames, grab_errors = _grab_frames(cam_ids)

        if len(frames) < 2:
            return jsonify({
                "error": "Need frames from at least 2 cameras to calibrate",
                "grab_errors": {str(k): v for k, v in grab_errors.items()},
            }), 422

        means: dict[int, np.ndarray] = {}
        for cam_id, frame in frames.items():
            bgr = _to_bgr(frame).astype(np.float32)
            gray = bgr.mean(axis=2)
            # Exclude near-black and near-saturated pixels for a cleaner mean.
            mask = (gray > 20) & (gray < 235)
            if mask.sum() < 100:
                mask = np.ones(gray.shape, dtype=bool)
            means[cam_id] = bgr[mask].mean(axis=0)  # [mean_B, mean_G, mean_R]

        ref_id = cam_ids[0]
        if ref_id not in means:
            return jsonify({"error": f"Could not grab frame from reference camera {ref_id}"}), 422

        ref_mean = means[ref_id]
        corrections: dict[str, dict] = {}
        for cam_id in cam_ids:
            if cam_id not in means:
                continue
            m = means[cam_id]
            corrections[str(cam_id)] = {
                "b": round(float(np.clip(ref_mean[0] / max(m[0], 1.0), 0.5, 2.0)), 4),
                "g": round(float(np.clip(ref_mean[1] / max(m[1], 1.0), 0.5, 2.0)), 4),
                "r": round(float(np.clip(ref_mean[2] / max(m[2], 1.0), 0.5, 2.0)), 4),
            }

        cal["color_correction"] = corrections
        cal["color_correction_calibrated_at"] = datetime.now(timezone.utc).isoformat()
        _save_calibration(cal)
        logger.info("stitch_color_calibrated", cameras=list(corrections.keys()), reference=ref_id)

        return jsonify({
            "calibrated": True,
            "reference_camera": ref_id,
            "corrections": corrections,
            "grab_errors": {str(k): v for k, v in grab_errors.items()},
        })

    @bp.route("/calibrate-color", methods=["DELETE"])
    def clear_color_calibration():
        """Remove colour correction data without touching the stitch calibration."""
        cal = _load_calibration()
        if cal is None or "color_correction" not in cal:
            return jsonify({"error": "No colour calibration found"}), 404
        cal.pop("color_correction", None)
        cal.pop("color_correction_calibrated_at", None)
        _save_calibration(cal)
        logger.info("stitch_color_calibration_cleared")
        return jsonify({"cleared": True})

    # ── Shared pre-flight check ────────────────────────────────────────────────

    def _calibration_preflight() -> tuple[dict | None, Response | None]:
        cal = _load_calibration()
        if cal is None:
            resp = jsonify({
                "error": "No calibration found",
                "hint": "POST /rpi/mindvision/stitch/calibrate with {\"cameras\": [id1, id2]}",
            })
            resp.status_code = 412
            return None, resp

        missing = [
            cid for cid in sorted(cameras.keys())
            if str(cid) not in cal.get("cameras", {})
        ]
        if missing:
            resp = jsonify({
                "error": f"Calibration incomplete — cameras {missing} not yet calibrated",
                "cameras_calibrated": sorted(int(k) for k in cal.get("cameras", {})),
                "cameras_missing": missing,
                "hint": f"POST /rpi/mindvision/stitch/calibrate with {{\"cameras\": {missing}}}",
            })
            resp.status_code = 412
            return None, resp

        return cal, None

    # ── Preview ────────────────────────────────────────────────────────────────

    @bp.route("/capture", methods=["GET"])
    def capture():
        """Capture all cameras and return a single stitched JPEG.

        Returns 412 with a helpful message if calibration is missing or incomplete.

        Query params:
          quality  int  JPEG quality 1–100 (default 85)
        """
        cal, err_resp = _calibration_preflight()
        if err_resp is not None:
            return err_resp

        quality = max(1, min(request.args.get("quality", 85, type=int), 100))
        max_width = request.args.get("max_width", 1280, type=int)
        cfg = _load_config()
        camera_order = cfg.get("camera_order") or None

        frames, grab_errors = _grab_frames(sorted(cameras.keys()))
        if not frames:
            return jsonify({
                "error": "No frames captured",
                "details": {str(k): v for k, v in grab_errors.items()},
            }), 503

        result = _stitch_frames(frames, cal, camera_order=camera_order, max_width=max_width)  # type: ignore[arg-type]
        if result is None:
            return jsonify({"error": "Stitching produced empty output"}), 500

        ok, buf = cv2.imencode(".jpg", result, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return jsonify({"error": "JPEG encode failed"}), 500

        return send_file(io.BytesIO(buf.tobytes()), mimetype="image/jpeg")

    # ── MJPEG stream ───────────────────────────────────────────────────────────

    @bp.route("/stream", methods=["GET"])
    def stream():
        """MJPEG stream — stitched composite when calibrated, single camera otherwise.

        Query params:
          fps        float  Frames per second (default 1, max 5)
          quality    int    JPEG quality 1–100 (default 75)
          max_width  int    Cap each input frame width before warping (default 640, 0 = no limit)
          camera_id  int    Fallback camera when not calibrated (default 0)
        """
        for cam in cameras.values():
            if isinstance(cam, MindVisionCamera) and cam.mode == CameraMode.HARDWARE_TRIGGER:
                return Response("Camera is in hardware trigger mode", status=409, mimetype="text/plain")

        cal = _load_calibration()
        all_ids = sorted(cameras.keys())
        calibrated_ids = sorted(int(k) for k in cal.get("cameras", {})) if cal else []
        is_fully_calibrated = cal is not None and all(cid in calibrated_ids for cid in all_ids)

        fps = max(0.1, min(request.args.get("fps", 1.0, type=float), 5.0))
        quality = max(1, min(request.args.get("quality", 75, type=int), 100))
        max_width = request.args.get("max_width", 640, type=int)
        frame_interval = 1.0 / fps

        if is_fully_calibrated:
            stream_cfg = _load_config()
            stream_camera_order = stream_cfg.get("camera_order") or None

            def generate():
                while True:
                    t0 = time.monotonic()
                    frames, _ = _grab_frames(all_ids)
                    if frames:
                        result = _stitch_frames(frames, cal, camera_order=stream_camera_order, max_width=max_width)  # type: ignore[arg-type]
                        if result is not None:
                            ok, buf = cv2.imencode(".jpg", result, [cv2.IMWRITE_JPEG_QUALITY, quality])
                            if ok:
                                yield (
                                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                    + buf.tobytes()
                                    + b"\r\n"
                                )
                    elapsed = time.monotonic() - t0
                    remaining = frame_interval - elapsed
                    if remaining > 0:
                        time.sleep(remaining)
        else:
            fallback_id = request.args.get("camera_id", 0, type=int)
            if fallback_id not in cameras:
                return jsonify({"error": f"Camera {fallback_id} not found"}), 404

            def generate():
                while True:
                    t0 = time.monotonic()
                    frames, _ = _grab_frames([fallback_id])
                    if fallback_id in frames:
                        bgr = _to_bgr(frames[fallback_id])
                        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
                        if ok:
                            yield (
                                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                + buf.tobytes()
                                + b"\r\n"
                            )
                    elapsed = time.monotonic() - t0
                    remaining = frame_interval - elapsed
                    if remaining > 0:
                        time.sleep(remaining)

        return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

    # ── Detection debug overlay ────────────────────────────────────────────────

    @bp.route("/detect", methods=["GET"])
    def detect():
        """Capture one camera and return a JPEG with detected ChArUco corners overlaid.

        Query params:
          camera_id   int   Camera index (default 0)
          quality     int   JPEG quality 1–100 (default 85)
          board_cols  int   Board columns (default 20)
          board_rows  int   Board rows (default 14)
          square_mm   float Checker square size in mm (default 10.0)
          marker_mm   float ArUco marker size in mm (default 8.0)
          aruco_dict  str   ArUco dictionary name (default "DICT_4X4_250")
        """
        cam_id = request.args.get("camera_id", 0, type=int)
        cam = cameras.get(cam_id)
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        quality = max(1, min(request.args.get("quality", 85, type=int), 100))
        config = _load_config()
        min_corners: int = config["min_corners"]

        try:
            board, aruco_dict_obj = _make_board(
                cols=request.args.get("board_cols", _DEFAULT_BOARD_COLS, type=int),
                rows=request.args.get("board_rows", _DEFAULT_BOARD_ROWS, type=int),
                square_mm=request.args.get("square_mm", _DEFAULT_SQUARE_MM, type=float),
                marker_mm=request.args.get("marker_mm", _DEFAULT_MARKER_MM, type=float),
                aruco_dict_name=request.args.get("aruco_dict", _DEFAULT_ARUCO_DICT),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        frames, errors = _grab_frames([cam_id])
        if cam_id not in frames:
            return jsonify({"error": errors.get(cam_id, "no frame")}), 503

        frame = frames[cam_id]
        gray = _to_gray(frame)
        corners, ids = _detect_charuco(gray, board, aruco_dict_obj)
        vis = _to_bgr(frame).copy()

        n_corners = 0
        if corners is not None and ids is not None:
            cv2.aruco.drawDetectedCornersCharuco(vis, corners, ids)
            n_corners = len(corners)

        if n_corners >= min_corners:
            color = (0, 200, 0)
            status = "ready"
        elif n_corners > 0:
            color = (0, 140, 220)
            status = "too few corners"
        else:
            color = (0, 60, 220)
            status = "not detected"

        label = f"cam{cam_id}: {n_corners} corners — {status}"
        cv2.putText(vis, label, (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 4)
        cv2.putText(vis, label, (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 2)

        ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return jsonify({"error": "JPEG encode failed"}), 500

        return send_file(io.BytesIO(buf.tobytes()), mimetype="image/jpeg")

    return bp
