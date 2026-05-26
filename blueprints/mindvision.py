"""Flask blueprint for MindVision-specific camera endpoints.

Registered only when the active camera is a MindVisionCamera.
All routes are prefixed with /rpi/mindvision.
"""
from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import zipfile
from collections import deque
from pathlib import Path

import config
import structlog
from camera.mindvision import CameraMode, MindVisionCamera
from flask import Blueprint, Response, after_this_request, jsonify, request, send_file

logger = structlog.get_logger()

# Per-camera rolling sharpness history for trend detection (camera_id -> deque).
_calibration_history: dict[int, deque] = {}

# Count of active calibration stream generators per camera. Used to avoid
# reverting the trigger mode while another calibration stream is still running.
_calibration_stream_count: dict[int, int] = {}

# ChArUco board for calibration stream detection overlay (lazy-initialised).
_charuco_board = None
_charuco_dict = None


def _get_charuco_board():
    global _charuco_board, _charuco_dict
    if _charuco_board is None:
        import cv2
        _charuco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
        _charuco_board = cv2.aruco.CharucoBoard((20, 14), 10.0, 8.0, _charuco_dict)
    return _charuco_board, _charuco_dict


def _render_calibration_overlay(
    frame: "np.ndarray",
    history: "deque[float]",
    peak_threshold: int,
    max_width: int,
    camera_id: int = 0,
    detect_charuco: bool = False,
) -> bytes:
    """Return a JPEG with focus peaking + info overlay.

    All text is stacked in the top-left corner.  The sharpness bar stays
    at the bottom.  Overlay lines (top to bottom):
      cam{id}
      Score: {n}  {trend arrow} {suggestion}
      Clipped: {n}%  {arrow} {exposure suggestion}
      ChArUco: {n} corners — {ready / too few / not detected}
    """
    import cv2 as _cv2
    import numpy as np
    from PIL import Image as PilImage, ImageDraw, ImageFont

    # frame is HxWx3 BGR (color) or HxWx1 (mono) from the SDK.
    if frame.ndim == 3 and frame.shape[2] == 3:
        pil_img = PilImage.fromarray(frame[:, :, ::-1])  # BGR -> RGB
    elif frame.ndim == 3 and frame.shape[2] == 1:
        pil_img = PilImage.fromarray(frame[:, :, 0], mode="L").convert("RGB")
    else:
        pil_img = PilImage.fromarray(frame, mode="L").convert("RGB")

    orig_w, orig_h = pil_img.size
    if orig_w > max_width:
        new_h = int(orig_h * max_width / orig_w)
        pil_img = pil_img.resize((max_width, new_h), PilImage.BILINEAR)

    w, h = pil_img.size
    rgb = np.array(pil_img, dtype=np.uint8)

    # Grayscale for gradient computation and ChArUco detection.
    gray = (
        0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    ).astype(np.float32)
    gray_u8 = gray.astype(np.uint8)

    # Laplacian variance on the center third — standard single-frame sharpness metric.
    roi_y1, roi_y2 = h // 3, 2 * h // 3
    roi_x1, roi_x2 = w // 3, 2 * w // 3
    roi = gray[roi_y1:roi_y2, roi_x1:roi_x2]
    lap = (
        roi[:-2, 1:-1] + roi[2:, 1:-1] + roi[1:-1, :-2] + roi[1:-1, 2:]
        - 4.0 * roi[1:-1, 1:-1]
    )
    score = float(np.var(lap))
    history.append(score)

    # Trend: compare first half vs. second half of rolling window.
    if len(history) >= 6:
        arr = list(history)
        mid = len(arr) // 2
        first_mean = float(np.mean(arr[:mid]))
        last_mean = float(np.mean(arr[mid:]))
        rel = (last_mean - first_mean) / max(abs(first_mean), 1.0)
        if rel > 0.05:
            trend_char, trend_color, suggestion = "↑", (80, 220, 80), "keep going"
        elif rel < -0.05:
            trend_char, trend_color, suggestion = "↓", (220, 80, 80), "reverse direction"
        else:
            trend_char, trend_color, suggestion = "●", (220, 220, 80), "at or near peak"
    else:
        trend_char, trend_color, suggestion = "~", (180, 180, 180), "measuring..."

    # Exposure analysis: fraction of pixels with any channel clipped (≥ 250).
    clip_mask = np.any(rgb >= 250, axis=2)
    clipped_pct = 100.0 * float(clip_mask.sum()) / (h * w)
    mean_brightness = float(gray.mean())

    if clipped_pct > 5.0:
        exp_char, exp_color, exp_suggestion = "▲", (220, 60, 60), "Close aperture"
    elif clipped_pct > 0.5:
        exp_char, exp_color, exp_suggestion = "▲", (220, 160, 40), "Slightly overexposed"
    elif mean_brightness < 30:
        exp_char, exp_color, exp_suggestion = "▼", (80, 160, 220), "Open aperture"
    elif mean_brightness < 60:
        exp_char, exp_color, exp_suggestion = "▼", (140, 200, 240), "Slightly underexposed"
    else:
        exp_char, exp_color, exp_suggestion = "●", (80, 220, 80), "Exposure OK"

    # ChArUco detection — only when explicitly requested.
    if detect_charuco:
        try:
            board, aruco_dict = _get_charuco_board()
            detector = _cv2.aruco.ArucoDetector(aruco_dict)
            marker_corners, marker_ids, _ = detector.detectMarkers(gray_u8)
            n_charuco = 0
            if marker_ids is not None and len(marker_ids) >= 4:
                _, charuco_corners, _ = _cv2.aruco.interpolateCornersCharuco(
                    marker_corners, marker_ids, gray_u8, board
                )
                if charuco_corners is not None:
                    n_charuco = len(charuco_corners)
        except Exception:
            n_charuco = -1

        if n_charuco < 0:
            charuco_text, charuco_color = "ChArUco: unavailable", (180, 180, 180)
        elif n_charuco >= 8:
            charuco_text, charuco_color = f"ChArUco: {n_charuco} corners — ready", (80, 220, 80)
        elif n_charuco > 0:
            charuco_text, charuco_color = f"ChArUco: {n_charuco} corners — too few", (220, 160, 40)
        else:
            charuco_text, charuco_color = "ChArUco: not detected", (220, 80, 80)
    else:
        charuco_text, charuco_color = None, None

    # Focus peaking: highlight pixels where |gradient| > threshold in magenta.
    gi = gray.astype(np.int32)
    gx = np.abs(gi[1:-1, 2:] - gi[1:-1, :-2])
    gy = np.abs(gi[2:, 1:-1] - gi[:-2, 1:-1])
    gradient = np.zeros((h, w), dtype=np.int32)
    gradient[1:-1, 1:-1] = gx + gy
    peak_mask = gradient > peak_threshold

    rgb_out = rgb.copy()
    rgb_out[peak_mask] = [255, 0, 255]
    # Clipped pixels painted red on top — more critical than focus-peak markers.
    rgb_out[clip_mask] = [255, 40, 40]

    pil_out = PilImage.fromarray(rgb_out).convert("RGBA")
    draw = ImageDraw.Draw(pil_out)

    font_size = max(16, w // 48)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size
        )
        font_sm = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max(12, font_size - 4)
        )
    except Exception:
        font = ImageFont.load_default()
        font_sm = font

    # ROI box.
    draw.rectangle([roi_x1, roi_y1, roi_x2, roi_y2], outline=(255, 255, 0), width=2)

    # ── Top-left info stack ───────────────────────────────────────────────────
    pad = max(8, w // 80)
    line_gap = font_size + 6
    y = pad

    lines = [
        (f"cam{camera_id}", (255, 255, 255)),
        (f"Score: {score:.0f}  {trend_char} {suggestion}", trend_color),
        (f"Clipped: {clipped_pct:.1f}%  {exp_char} {exp_suggestion}", exp_color),
        *(([(charuco_text, charuco_color)]) if charuco_text else []),
    ]
    for text, color in lines:
        draw.text((pad, y), text, fill=color, font=font)
        y += line_gap

    # ── Sharpness bar (bottom) ────────────────────────────────────────────────
    max_score = max(list(history) + [1.0])
    bar_total = w // 3
    bar_x1 = pad
    bar_y2 = h - pad
    bar_y1 = bar_y2 - max(14, font_size // 2)
    fill_frac = min(score / max_score, 1.0)
    fill_x2 = bar_x1 + int(fill_frac * bar_total)
    bar_fill = (
        (80, 200, 80) if fill_frac > 0.8 else
        (220, 200, 60) if fill_frac > 0.5 else
        (200, 80, 80)
    )
    draw.rectangle([bar_x1, bar_y1, bar_x1 + bar_total, bar_y2], outline=(200, 200, 200), width=1)
    if fill_x2 > bar_x1:
        draw.rectangle([bar_x1 + 1, bar_y1 + 1, fill_x2, bar_y2 - 1], fill=bar_fill)
    draw.text(
        (bar_x1, bar_y1 - font_size - 2),
        "Sharpness (relative to max seen)",
        fill=(200, 200, 200),
        font=font_sm,
    )

    buf = io.BytesIO()
    pil_out.convert("RGB").save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def create_blueprint(cameras: dict[int, MindVisionCamera]) -> Blueprint:
    bp = Blueprint("mindvision", __name__, url_prefix="/rpi/mindvision")

    from camera.mindvision_trigger import SerialTriggerListener, check_server_health
    _serial_listener = SerialTriggerListener(cameras)

    def _resolve_camera():
        cam_id = request.args.get("camera_id", 0, type=int)
        cam = cameras.get(cam_id)
        return cam, cam_id

    # ── Camera discovery ──────────────────────────────────────────────────────

    @bp.route("/cameras", methods=["GET"])
    def list_cameras():
        """Return every known camera with its index and serial number.

        Use this to discover which camera_id corresponds to which physical
        camera (identified by serial_number).  Pass camera_id to all other
        endpoints once you know the mapping.
        """
        return jsonify([
            {
                "camera_id": cam_id,
                "serial_number": cam.serial_number,
                "model": cam.camera_info().get("model"),
                "product_name": cam.camera_info().get("product_name"),
                "port_type": cam.camera_info().get("port_type"),
                "status": "open" if cam.serial_number is not None else "closed",
            }
            for cam_id, cam in sorted(cameras.items())
        ])

    # ── Mode ──────────────────────────────────────────────────────────────────

    @bp.route("/mode", methods=["GET"])
    def get_mode():
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        return jsonify({"camera_id": cam_id, "mode": cam.mode.value})

    @bp.route("/mode", methods=["POST"])
    def set_mode():
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        body = request.get_json(silent=True) or {}
        raw = body.get("mode", "")
        try:
            mode = CameraMode(raw)
        except ValueError:
            valid = [m.value for m in CameraMode]
            return jsonify({"error": f"Invalid mode '{raw}'. Valid: {valid}"}), 400
        try:
            cam.set_mode(mode)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        logger.info("mode_switch_requested", camera_id=cam_id, mode=mode.value)
        return jsonify({"camera_id": cam_id, "mode": mode.value})

    # ── White balance ─────────────────────────────────────────────────────────

    @bp.route("/white-balance", methods=["GET"])
    def get_white_balance():
        import mvsdk
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"calibrated": False})
        auto = mvsdk.CameraGetWbMode(cam._h_camera)
        r, g, b = mvsdk.CameraGetGain(cam._h_camera)
        return jsonify({"calibrated": True, "auto": bool(auto), "r_gain": r, "g_gain": g, "b_gain": b})

    @bp.route("/calibrate-wb", methods=["POST"])
    def calibrate_wb():
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        try:
            gains = cam.calibrate_white_balance()
            return jsonify(gains)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        except Exception:
            logger.exception("calibrate_wb_failed", camera_id=cam_id)
            return jsonify({"error": "Calibration failed"}), 500

    # ── Rotation / mirror ─────────────────────────────────────────────────────

    @bp.route("/orientation", methods=["GET"])
    def get_orientation():
        """Return current rotation and mirror state for a camera.

        Query params:
          camera_id  int  Camera index (default 0)

        Response:
          rotation   int   0=0°, 1=90°CCW, 2=180°, 3=270°CCW
          h_mirror   bool  Horizontal mirror enabled
          v_mirror   bool  Vertical mirror enabled
        """
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404
        try:
            orientation = cam.get_orientation()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        return jsonify({"camera_id": cam_id, **orientation})

    @bp.route("/rotation", methods=["POST"])
    def set_rotation():
        """Set SDK rotation for a camera and persist to device config.

        JSON body:
          rotation  int  0=0°, 1=90°CCW, 2=180°, 3=270°CCW

        Query params:
          camera_id  int  Camera index (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        body = request.get_json(silent=True) or {}
        rotation = body.get("rotation")
        if rotation is None or rotation not in (0, 1, 2, 3):
            return jsonify({"error": "rotation must be 0, 1, 2, or 3"}), 400
        try:
            cam.set_rotation(rotation)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        logger.info("rotation_set", camera_id=cam_id, rotation=rotation)
        return jsonify({"camera_id": cam_id, "rotation": rotation})

    @bp.route("/mirror", methods=["POST"])
    def set_mirror():
        """Set SDK mirror for a camera and persist to device config.

        JSON body:
          direction  str  "horizontal" or "vertical"
          enable     bool

        Query params:
          camera_id  int  Camera index (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        body = request.get_json(silent=True) or {}
        direction_str = body.get("direction", "")
        enable = body.get("enable")
        if direction_str not in ("horizontal", "vertical"):
            return jsonify({"error": "direction must be 'horizontal' or 'vertical'"}), 400
        if not isinstance(enable, bool):
            return jsonify({"error": "enable must be a boolean"}), 400
        direction = 0 if direction_str == "horizontal" else 1
        try:
            cam.set_mirror(direction, enable)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        logger.info("mirror_set", camera_id=cam_id, direction=direction_str, enabled=enable)
        return jsonify({"camera_id": cam_id, "direction": direction_str, "enable": enable})

    # ── Multi-camera capture ───────────────────────────────────────────────────

    @bp.route("/capture-all", methods=["POST"])
    def capture_all():
        """Capture one frame from every camera simultaneously and return a zip.

        For hardware-trigger mode all grab threads block together — the physical
        signal releases them all at once.  For continuous/capture mode each
        thread grabs the next available frame independently.
        """
        results: dict[int, Path] = {}
        errors: dict[int, str] = {}
        mu = threading.Lock()

        config.CAPTURE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=config.CAPTURE_TMP_DIR))

        def grab_one(cam_id: int, cam: MindVisionCamera) -> None:
            try:
                path, _ = cam.capture_image(output_folder=tmp_dir)
                with mu:
                    results[cam_id] = path
            except Exception as e:
                with mu:
                    errors[cam_id] = str(e)

        threads = [
            threading.Thread(target=grab_one, args=(cam_id, cam), daemon=True)
            for cam_id, cam in cameras.items()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        if errors:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.warning("capture_all_partial_failure", errors={str(k): v for k, v in errors.items()})
            return jsonify({
                "error": "One or more cameras failed to capture",
                "details": {str(k): v for k, v in errors.items()},
            }), 500

        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
                for cam_id in sorted(results):
                    zf.write(results[cam_id], f"camera_{cam_id}.jpg")
            buf.seek(0)

            @after_this_request
            def cleanup(response):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return response

            logger.info("capture_all_complete", camera_ids=sorted(results))
            return send_file(
                buf,
                mimetype="application/zip",
                as_attachment=True,
                download_name="capture_all.zip",
            )
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.exception("capture_all_zip_failed")
            return jsonify({"error": "Failed to build capture archive"}), 500

    # ── Calibration stream ────────────────────────────────────────────────────

    @bp.route("/calibration/stream")
    def calibration_stream():
        """MJPEG stream with focus peaking, sharpness score, and exposure overlay.

        Query params:
          camera_id       int   Camera index (default 0)
          fps             float Frames per second for the overlay stream (default 2, max 10)
          peak_threshold  int   Gradient magnitude threshold for peaking highlights (default 50)
          max_width       int   Downscale frames to this width before overlay (default 1280)
          charuco         int   Set to 1 to enable ChArUco board detection overlay (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        fps = max(0.5, min(request.args.get("fps", 2.0, type=float), 10.0))
        peak_threshold = request.args.get("peak_threshold", 50, type=int)
        max_width = request.args.get("max_width", 1280, type=int)
        detect_charuco = request.args.get("charuco", 0, type=int) == 1

        if cam_id not in _calibration_history:
            _calibration_history[cam_id] = deque(maxlen=30)
        history = _calibration_history[cam_id]

        _calibration_stream_count[cam_id] = _calibration_stream_count.get(cam_id, 0) + 1

        frame_interval = 1.0 / fps

        def generate():
            import mvsdk

            _continuous_started = False
            try:
                while True:
                    loop_start = time.monotonic()

                    if cam._h_camera is None:
                        time.sleep(1.0)
                        continue

                    # Switch to continuous mode if the main stream isn't already running.
                    if (
                        not cam._streaming
                        and not _continuous_started
                        and cam.mode != CameraMode.HARDWARE_TRIGGER
                    ):
                        mvsdk.CameraSetTriggerMode(cam._h_camera, 0)
                        _continuous_started = True
                        time.sleep(0.1)

                    with cam._lock:
                        frame = cam._grab_frame()

                    if frame is not None:
                        jpeg = _render_calibration_overlay(frame, history, peak_threshold, max_width, cam_id, detect_charuco)
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n"
                            + jpeg
                            + b"\r\n"
                        )

                    elapsed = time.monotonic() - loop_start
                    remaining = frame_interval - elapsed
                    if remaining > 0:
                        time.sleep(remaining)
            finally:
                _calibration_stream_count[cam_id] = _calibration_stream_count.get(cam_id, 1) - 1
                # Only revert to software trigger when the last calibration stream exits
                # and the main stream isn't running — mirrors the logic in stream_frames().
                if (
                    _continuous_started
                    and _calibration_stream_count.get(cam_id, 0) == 0
                    and cam._h_camera is not None
                    and not cam._streaming
                ):
                    try:
                        mvsdk.CameraSetTriggerMode(cam._h_camera, 1)
                    except Exception:
                        pass

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    # ── Serial trigger (Arduino encoder) ─────────────────────────────────────

    @bp.route("/serial-trigger/start", methods=["POST"])
    def serial_trigger_start():
        """Start listening for Arduino encoder trigger events over serial.

        Reads JSON lines from the configured serial port and captures + uploads
        an image on each trigger event.  All cameras are switched to
        hardware_trigger mode so the SDK waits for the physical trigger signal
        from the Arduino (D9) rather than issuing a software trigger.

        Optional JSON body:
          port  str  Serial device path (default: hw_trigger.serial_port from config)
          baud  int  Baud rate (default: hw_trigger.serial_baud from config)
        """
        if _serial_listener.running:
            return jsonify({"error": "Serial trigger listener is already running"}), 409

        body = request.get_json(silent=True) or {}
        port = body.get("port", config.HW_TRIGGER_SERIAL_PORT)
        baud = body.get("baud", config.HW_TRIGGER_SERIAL_BAUD)

        # Switch every camera to hardware trigger mode before opening serial.
        mode_errors = {}
        for cam_id, cam in cameras.items():
            try:
                cam.set_mode(CameraMode.HARDWARE_TRIGGER)
            except Exception as exc:
                mode_errors[cam_id] = str(exc)

        if mode_errors:
            return jsonify({"error": "Failed to set hardware trigger mode", "details": {str(k): v for k, v in mode_errors.items()}}), 503

        try:
            _serial_listener.start(port=port, baud=int(baud))
        except Exception as exc:
            # Revert cameras if the serial port failed to open.
            for cam in cameras.values():
                try:
                    cam.set_mode(CameraMode.CAPTURE)
                except Exception:
                    pass
            logger.exception("serial_trigger_start_failed")
            return jsonify({"error": str(exc)}), 500

        health = check_server_health()
        logger.info("serial_trigger_started", port=port, baud=baud, cameras=list(cameras.keys()))
        return jsonify({"running": True, "port": port, "baud": baud, "camera_mode": CameraMode.HARDWARE_TRIGGER.value, "server_health": health})

    @bp.route("/serial-trigger/stop", methods=["POST"])
    def serial_trigger_stop():
        """Stop the serial trigger listener and revert cameras to capture mode."""
        if not _serial_listener.running:
            return jsonify({"error": "Serial trigger listener is not running"}), 409

        _serial_listener.stop()

        # Revert all cameras back to capture (software trigger) mode.
        for cam_id, cam in cameras.items():
            try:
                cam.set_mode(CameraMode.CAPTURE)
            except Exception as exc:
                logger.warning("serial_trigger_mode_revert_failed", camera_id=cam_id, error=str(exc))

        logger.info("serial_trigger_stopped")
        return jsonify({"running": False, "camera_mode": CameraMode.CAPTURE.value})

    @bp.route("/serial-trigger/status", methods=["GET"])
    def serial_trigger_status():
        """Return the current status and statistics of the serial trigger listener."""
        return jsonify(_serial_listener.status())

    @bp.route("/hw-trigger/diag", methods=["GET"])
    def hw_trigger_diag():
        """Diagnostic: report trigger mode and frame stats for every camera.

        Also fires a software trigger on each camera (regardless of current mode)
        and reports whether a frame was received.  Use this to confirm the cameras
        are alive and the SDK can grab frames independently of the hardware pin.
        """
        import mvsdk as _mvsdk
        results = {}
        for cam_id, cam in sorted(cameras.items()):
            if cam._h_camera is None:
                results[cam_id] = {"error": "camera not open"}
                continue
            try:
                trigger_mode = _mvsdk.CameraGetTriggerMode(cam._h_camera)
            except Exception as exc:
                trigger_mode = f"error: {exc}"
            stat = _mvsdk.CameraGetFrameStatistic(cam._h_camera)
            # Fire a software trigger and attempt a grab to confirm camera health.
            sw_ok = False
            sw_error = None
            try:
                _mvsdk.CameraSoftTrigger(cam._h_camera)
                raw, head = _mvsdk.CameraGetImageBuffer(cam._h_camera, 2000)
                _mvsdk.CameraReleaseImageBuffer(cam._h_camera, raw)
                sw_ok = True
            except Exception as exc:
                sw_error = str(exc)
            results[cam_id] = {
                "trigger_mode": trigger_mode,
                "frames_total": stat.iTotal,
                "frames_lost": stat.iLost,
                "sw_trigger_ok": sw_ok,
                "sw_trigger_error": sw_error,
            }
        return jsonify(results)

    @bp.route("/hw-trigger/server-health", methods=["GET"])
    def hw_trigger_server_health():
        """Check reachability of the hw_trigger upload server via health_check_url.

        Returns {"reachable": true, "status_code": 200} on success,
        {"reachable": false, "error": "..."} on failure, or
        {"reachable": null} if no health_check_url is configured.
        """
        result = check_server_health()
        return jsonify({"reachable": None} if result is None else result)

    @bp.route("/calibration/score")
    def calibration_score():
        """Return the current sharpness score and trend as JSON (single frame).

        Useful for scripted calibration loops. Uses the same rolling history as
        the calibration stream, so calling both simultaneously gives consistent trends.

        Query params:
          camera_id  int  Camera index (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        if cam_id not in _calibration_history:
            _calibration_history[cam_id] = deque(maxlen=30)
        history = _calibration_history[cam_id]

        try:
            import mvsdk
            import numpy as np

            with cam._lock:
                if not cam._streaming:
                    mvsdk.CameraSoftTrigger(cam._h_camera)
                frame = cam._grab_frame()
        except Exception as exc:
            return jsonify({"error": str(exc)}), 503

        if frame is None:
            return jsonify({"error": "No frame available"}), 503

        h, w = frame.shape[:2]
        if frame.ndim == 3 and frame.shape[2] == 3:
            gray = (
                0.114 * frame[:, :, 0] + 0.587 * frame[:, :, 1] + 0.299 * frame[:, :, 2]
            ).astype(np.float32)
        else:
            gray = frame[:, :, 0].astype(np.float32) if frame.ndim == 3 else frame.astype(np.float32)

        roi_y1, roi_y2 = h // 3, 2 * h // 3
        roi_x1, roi_x2 = w // 3, 2 * w // 3
        roi = gray[roi_y1:roi_y2, roi_x1:roi_x2]
        lap = (
            roi[:-2, 1:-1] + roi[2:, 1:-1] + roi[1:-1, :-2] + roi[1:-1, 2:]
            - 4.0 * roi[1:-1, 1:-1]
        )
        score = float(np.var(lap))
        history.append(score)

        trend = "measuring"
        suggestion = "keep adjusting and watch the score"
        if len(history) >= 6:
            arr = list(history)
            mid = len(arr) // 2
            first_mean = float(np.mean(arr[:mid]))
            last_mean = float(np.mean(arr[mid:]))
            rel = (last_mean - first_mean) / max(abs(first_mean), 1.0)
            if rel > 0.05:
                trend, suggestion = "increasing", "keep going"
            elif rel < -0.05:
                trend, suggestion = "decreasing", "reverse direction"
            else:
                trend, suggestion = "stable", "at or near peak focus"

        return jsonify({
            "camera_id": cam_id,
            "score": round(score, 2),
            "trend": trend,
            "suggestion": suggestion,
            "history_length": len(history),
            "roi": {"x1": roi_x1, "y1": roi_y1, "x2": roi_x2, "y2": roi_y2},
        })

    return bp
