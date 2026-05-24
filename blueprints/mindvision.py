"""Flask blueprint for MindVision-specific camera endpoints.

Registered only when the active camera is a MindVisionCamera.
All routes are prefixed with /rpi/mindvision.
"""
from __future__ import annotations

import io
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

import calibration
import config
import structlog
from camera.mindvision import CameraMode, MindVisionCamera
from flask import Blueprint, after_this_request, jsonify, request, send_file

logger = structlog.get_logger()


def create_blueprint(cameras: dict[int, MindVisionCamera]) -> Blueprint:
    bp = Blueprint("mindvision", __name__, url_prefix="/rpi/mindvision")

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
        wb = calibration.load().get("white_balance")
        if wb is None:
            return jsonify({"calibrated": False})
        return jsonify({"calibrated": True, **wb})

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

    return bp
