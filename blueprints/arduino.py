"""Arduino decoder/trigger control — serial listener lifecycle, parameter config,
and hardware diagnostics.

All routes are under /api/decoder.

Start/stop routes switch camera trigger mode as well as opening the serial port,
so they need access to both the listener and the camera registry.
"""
from __future__ import annotations

import os

import structlog
from flask import Blueprint, jsonify, request

import config
from camera.mindvision import CameraMode, MindVisionCamera
from camera.mindvision_trigger import (
    ARDUINO_DEFAULTS,
    ARDUINO_SETTABLE_KEYS,
    PHYSICAL_DEFAULTS,
    PHYSICAL_SETTABLE_KEYS,
    SerialTriggerListener,
    check_server_health,
    compute_arduino_params,
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
        status = listener.status()
        status["port_present"] = os.path.exists(config.HW_TRIGGER_SERIAL_PORT)
        return jsonify(status)

    @bp.route("/detect", methods=["POST"])
    def decoder_detect():
        """Probe the serial port; auto-start the listener if the Arduino is found."""
        port_present = os.path.exists(config.HW_TRIGGER_SERIAL_PORT)
        status = listener.status()
        status["port_present"] = port_present

        if not port_present or listener.running:
            return jsonify(status)

        # Port found and listener not running — set camera mode and auto-start.
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
            listener.start(port=config.HW_TRIGGER_SERIAL_PORT, baud=config.HW_TRIGGER_SERIAL_BAUD)
        except Exception as exc:
            logger.exception("decoder_detect_start_failed")
            return jsonify({"error": str(exc)}), 500

        logger.info("decoder_detect_auto_started", port=config.HW_TRIGGER_SERIAL_PORT)
        status = listener.status()
        status["port_present"] = True
        return jsonify(status)

    # ── Operating mode switch ──────────────────────────────────────────────────

    @bp.route("/mode/hw-trigger", methods=["POST"])
    def set_mode_hw_trigger():
        """Switch to hardware trigger mode: cameras → HARDWARE_TRIGGER, Arduino begins firing pulses."""
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
            listener.send_command({"cmd": "set_trigger_enabled", "value": True})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        listener.set_trigger_state(True)
        return jsonify({"trigger_enabled": True, "camera_mode": CameraMode.HARDWARE_TRIGGER.value})

    @bp.route("/mode/calibration", methods=["POST"])
    def set_mode_calibration():
        """Switch to calibration mode: cameras → CAPTURE (software trigger), Arduino stops firing pulses."""
        for cam_id, cam in cameras.items():
            try:
                cam.set_mode(CameraMode.CAPTURE)
            except Exception as exc:
                logger.warning("set_mode_calibration_revert_failed", camera_id=cam_id, error=str(exc))

        try:
            listener.send_command({"cmd": "set_trigger_enabled", "value": False})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        listener.set_trigger_state(False)
        return jsonify({"trigger_enabled": False, "camera_mode": CameraMode.CAPTURE.value})

    @bp.route("/trigger/fire", methods=["POST"])
    def trigger_fire():
        """Send a software trigger over serial — fires one pulse immediately on the Arduino."""
        if not listener.running:
            return jsonify({"error": "Decoder listener is not running"}), 409
        if not listener.serial_connected:
            return jsonify({"error": "Arduino serial port not connected"}), 503
        try:
            listener.send_command({"cmd": "fire_trigger"})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"fired": True})

    @bp.route("/reset-count", methods=["POST"])
    def reset_count():
        """Reset the Arduino encoder count and speed to zero."""
        try:
            listener.send_command({"cmd": "reset_count"})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"reset": True})

    # ── Arduino parameter config ───────────────────────────────────────────────

    @bp.route("/config", methods=["GET"])
    def get_config():
        """Return current Arduino parameters and the physical wheel/encoder configuration."""
        status = listener.status()
        saved = listener.file_config()
        physical = {k: saved.get(k, PHYSICAL_DEFAULTS[k]) for k in PHYSICAL_DEFAULTS}
        return jsonify({
            "arduino_config": status.get("arduino_config", {}),
            "physical_config": physical,
            "trigger_enabled": status.get("trigger_enabled"),
            "arduino_defaults": ARDUINO_DEFAULTS,
            "physical_defaults": PHYSICAL_DEFAULTS,
        })

    @bp.route("/config", methods=["PATCH"])
    def patch_config():
        """Update physical wheel/encoder params or raw Arduino params.

        Physical keys (wheel_diameter_mm, encoder_ppr, capture_interval_mm)
        automatically recompute and push counts_per_cm + trigger_interval to the Arduino.

        Raw keys (pulse_width_ms, speed_report_interval_ms) are sent directly.
        """
        body = request.get_json(silent=True) or {}
        if not body:
            return jsonify({"error": "Request body must be a JSON object"}), 400

        ALL_SETTABLE = {**PHYSICAL_SETTABLE_KEYS, **ARDUINO_SETTABLE_KEYS}

        errors: dict = {}
        updates: dict = {}
        for key, value in body.items():
            if key not in ALL_SETTABLE:
                errors[key] = "not a settable key"
                continue
            try:
                updates[key] = ALL_SETTABLE[key](value)
            except (ValueError, TypeError):
                errors[key] = f"expected {ALL_SETTABLE[key].__name__}"

        if errors:
            return jsonify({"error": "invalid values", "details": errors}), 400

        # If any physical param changed, recompute and push derived Arduino params.
        physical_keys = PHYSICAL_SETTABLE_KEYS.keys()
        if updates.keys() & physical_keys:
            saved = listener.file_config()
            merged = {k: saved.get(k, PHYSICAL_DEFAULTS[k]) for k in PHYSICAL_DEFAULTS}
            merged.update({k: v for k, v in updates.items() if k in physical_keys})
            derived = compute_arduino_params(
                diameter_mm=merged["wheel_diameter_mm"],
                ppr=int(merged["encoder_ppr"]),
                interval_mm=merged["capture_interval_mm"],
            )
            updates.update(derived)

        to_save = dict(updates)
        not_running = []
        for key, value in updates.items():
            if key in ARDUINO_SETTABLE_KEYS:
                try:
                    listener.send_command({"cmd": f"set_{key}", "value": value})
                except RuntimeError:
                    not_running.append(key)

        listener.save_config(to_save)

        result: dict = {"updated": list(to_save.keys())}
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

    # ── Simulator ─────────────────────────────────────────────────────────────

    @bp.route("/simulator/start", methods=["POST"])
    def simulator_start():
        """Start the decoder simulator.

        Sends fire_trigger commands to the Arduino at the interval derived from
        capture_interval_mm ÷ speed_cms. The serial listener must already be
        running (i.e. Arduino connected and decoder started).

        Optional JSON body:
          speed_cms  float  Simulated belt speed in cm/s (default: 5.0)
        """
        body = request.get_json(silent=True) or {}
        try:
            speed_cms = float(body.get("speed_cms", 5.0))
        except (ValueError, TypeError):
            return jsonify({"error": "speed_cms must be a number"}), 400
        if speed_cms <= 0:
            return jsonify({"error": "speed_cms must be positive"}), 400

        try:
            listener.start_simulator(speed_cms)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

        logger.info("simulator_start_api", speed_cms=speed_cms)
        return jsonify({"simulator_running": True, "speed_cms": speed_cms})

    @bp.route("/simulator/stop", methods=["POST"])
    def simulator_stop():
        """Stop the decoder simulator."""
        if not listener.simulator_running:
            return jsonify({"error": "Simulator is not running"}), 409

        listener.stop_simulator()
        logger.info("simulator_stop_api")
        return jsonify({"simulator_running": False})

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
