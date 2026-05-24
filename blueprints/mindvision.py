"""Flask blueprint for MindVision-specific camera endpoints.

Registered only when the active camera is a MindVisionCamera.
All routes are prefixed with /rpi/mindvision.
"""
from __future__ import annotations

import calibration
import structlog
from camera.mindvision import CameraMode, MindVisionCamera
from flask import Blueprint, jsonify, request

logger = structlog.get_logger()


def create_blueprint(camera: MindVisionCamera) -> Blueprint:
    bp = Blueprint("mindvision", __name__, url_prefix="/rpi/mindvision")

    # ── Mode ──────────────────────────────────────────────────────────────────

    @bp.route("/mode", methods=["GET"])
    def get_mode():
        return jsonify({"mode": camera.mode.value})

    @bp.route("/mode", methods=["POST"])
    def set_mode():
        body = request.get_json(silent=True) or {}
        raw = body.get("mode", "")
        try:
            mode = CameraMode(raw)
        except ValueError:
            valid = [m.value for m in CameraMode]
            return jsonify({"error": f"Invalid mode '{raw}'. Valid: {valid}"}), 400
        try:
            camera.set_mode(mode)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        logger.info("mode_switch_requested", mode=mode.value)
        return jsonify({"mode": mode.value})

    # ── White balance ─────────────────────────────────────────────────────────

    @bp.route("/white-balance", methods=["GET"])
    def get_white_balance():
        wb = calibration.load().get("white_balance")
        if wb is None:
            return jsonify({"calibrated": False})
        return jsonify({"calibrated": True, **wb})

    @bp.route("/calibrate-wb", methods=["POST"])
    def calibrate_wb():
        try:
            gains = camera.calibrate_white_balance()
            return jsonify(gains)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        except Exception:
            logger.exception("calibrate_wb_failed")
            return jsonify({"error": "Calibration failed"}), 500

    return bp
