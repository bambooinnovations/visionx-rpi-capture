"""Arduino decoder/trigger control — serial listener lifecycle, parameter config,
and hardware diagnostics.

All routes are under /api/decoder.

Start/stop routes switch camera trigger mode as well as opening the serial port,
so they need access to both the listener and the camera registry.
"""
from __future__ import annotations

import structlog
from flask import Blueprint, jsonify, request

import config
from camera.mindvision import CameraMode, MindVisionCamera
from camera.mindvision_trigger import (
    ARDUINO_DEFAULTS,
    ARDUINO_SETTABLE_KEYS,
    SerialTriggerListener,
    check_server_health,
)

logger = structlog.get_logger()


def create_blueprint(
    listener: SerialTriggerListener,
    cameras: dict[int, MindVisionCamera],
) -> Blueprint:
    bp = Blueprint("arduino", __name__, url_prefix="/api/decoder")

    # ── Serial listener lifecycle ──────────────────────────────────────────────

    @bp.route("/start", methods=["POST"])
    def decoder_start():
        """Start the serial trigger listener and switch cameras to hardware trigger mode.

        Optional JSON body:
          port  str  Serial device path (default: hw_trigger.serial_port from config)
          baud  int  Baud rate (default: hw_trigger.serial_baud from config)
        """
        if listener.running:
            return jsonify({"error": "Decoder listener is already running"}), 409

        body = request.get_json(silent=True) or {}
        port = body.get("port", config.HW_TRIGGER_SERIAL_PORT)
        baud = body.get("baud", config.HW_TRIGGER_SERIAL_BAUD)

        mode_errors = {}
        for cam_id, cam in cameras.items():
            try:
                cam.set_mode(CameraMode.HARDWARE_TRIGGER)
            except Exception as exc:
                mode_errors[cam_id] = str(exc)

        if mode_errors:
            return jsonify({
                "error": "Failed to set hardware trigger mode",
                "details": {str(k): v for k, v in mode_errors.items()},
            }), 503

        try:
            listener.start(port=port, baud=int(baud))
        except Exception as exc:
            for cam in cameras.values():
                try:
                    cam.set_mode(CameraMode.CAPTURE)
                except Exception:
                    pass
            logger.exception("decoder_start_failed")
            return jsonify({"error": str(exc)}), 500

        health = check_server_health()
        logger.info("decoder_started", port=port, baud=baud, cameras=list(cameras.keys()))
        return jsonify({
            "running": True,
            "port": port,
            "baud": baud,
            "camera_mode": CameraMode.HARDWARE_TRIGGER.value,
            "server_health": health,
        })

    @bp.route("/stop", methods=["POST"])
    def decoder_stop():
        """Stop the serial trigger listener and revert cameras to capture mode."""
        if not listener.running:
            return jsonify({"error": "Decoder listener is not running"}), 409

        listener.stop()

        for cam_id, cam in cameras.items():
            try:
                cam.set_mode(CameraMode.CAPTURE)
            except Exception as exc:
                logger.warning("decoder_stop_mode_revert_failed", camera_id=cam_id, error=str(exc))

        logger.info("decoder_stopped")
        return jsonify({"running": False, "camera_mode": CameraMode.CAPTURE.value})

    @bp.route("/status", methods=["GET"])
    def decoder_status():
        """Full listener status including live Arduino state and capture statistics."""
        return jsonify(listener.status())

    # ── Arduino trigger on/off ─────────────────────────────────────────────────

    @bp.route("/trigger/enable", methods=["POST"])
    def trigger_enable():
        """Tell the Arduino to start firing trigger pulses (distance-based)."""
        try:
            listener.send_command({"cmd": "set_trigger_enabled", "value": True})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"trigger_enabled": True})

    @bp.route("/trigger/disable", methods=["POST"])
    def trigger_disable():
        """Tell the Arduino to stop firing trigger pulses without stopping the listener."""
        try:
            listener.send_command({"cmd": "set_trigger_enabled", "value": False})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"trigger_enabled": False})

    @bp.route("/trigger/fire", methods=["POST"])
    def trigger_fire():
        """Send a software trigger over serial — fires one pulse immediately on the Arduino."""
        try:
            listener.send_command({"cmd": "fire_trigger"})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"fired": True})

    # ── Arduino parameter config ───────────────────────────────────────────────

    @bp.route("/config", methods=["GET"])
    def get_config():
        """Return the current Arduino parameters as last reported by the Arduino."""
        status = listener.status()
        return jsonify({
            "arduino_config": status.get("arduino_config", {}),
            "trigger_enabled": status.get("trigger_enabled"),
            "defaults": ARDUINO_DEFAULTS,
        })

    @bp.route("/config", methods=["PATCH"])
    def patch_config():
        """Update one or more Arduino parameters, apply immediately, and persist to file.

        Settable keys: trigger_interval (int), counts_per_cm (float),
                       pulse_width_ms (int), speed_report_interval_ms (int).
        """
        body = request.get_json(silent=True) or {}
        if not body:
            return jsonify({"error": "Request body must be a JSON object"}), 400

        errors: dict = {}
        updates: dict = {}
        for key, value in body.items():
            if key not in ARDUINO_SETTABLE_KEYS:
                errors[key] = "not a settable key"
                continue
            try:
                updates[key] = ARDUINO_SETTABLE_KEYS[key](value)
            except (ValueError, TypeError):
                errors[key] = f"expected {ARDUINO_SETTABLE_KEYS[key].__name__}"

        if errors:
            return jsonify({"error": "invalid values", "details": errors}), 400

        not_running = []
        for key, value in updates.items():
            try:
                listener.send_command({"cmd": f"set_{key}", "value": value})
            except RuntimeError:
                not_running.append(key)

        listener.save_config(updates)

        result: dict = {"updated": list(updates.keys())}
        if not_running:
            result["warning"] = f"Saved to file but listener not running — {not_running} will apply on next connect"
        return jsonify(result)

    @bp.route("/config", methods=["DELETE"])
    def reset_config():
        """Delete arduino_config.json so Arduino compile-time defaults take effect on next connect."""
        listener.reset_config()
        for key, value in ARDUINO_DEFAULTS.items():
            try:
                listener.send_command({"cmd": f"set_{key}", "value": value})
            except RuntimeError:
                pass
        return jsonify({"reset": True, "defaults": ARDUINO_DEFAULTS})

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @bp.route("/diag", methods=["GET"])
    def decoder_diag():
        """Report trigger mode and frame stats for every camera.

        Also fires a software trigger on each camera and reports whether a frame
        was received, to confirm camera health independently of the hardware pin.
        """
        import mvsdk as _mvsdk

        results: dict = {}
        for cam_id, cam in sorted(cameras.items()):
            if cam._h_camera is None:
                results[cam_id] = {"error": "camera not open"}
                continue
            try:
                trigger_mode = _mvsdk.CameraGetTriggerMode(cam._h_camera)
            except Exception as exc:
                trigger_mode = f"error: {exc}"
            stat = _mvsdk.CameraGetFrameStatistic(cam._h_camera)
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

    @bp.route("/server-health", methods=["GET"])
    def decoder_server_health():
        """Check reachability of the hw_trigger upload server.

        Returns {"reachable": true} on success, {"reachable": false, "error": "..."} on
        failure, or {"reachable": null} if no health_check_url is configured.
        """
        result = check_server_health()
        return jsonify({"reachable": None} if result is None else result)

    return bp
