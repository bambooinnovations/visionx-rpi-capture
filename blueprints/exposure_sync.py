"""Experimental exposure-sync calibration blueprint.

Fixes cross-camera exposure drift on stations with multiple MindVision
cameras: each camera's auto-exposure (AE) normally converges independently,
which is fine for cameras whose framing includes a fixed background
reference but causes visible frame-to-frame brightness/color instability on
cameras whose whole frame is featureless material (nothing for whole-frame
AE metering to anchor on).

This feature lets an operator designate one camera as the AE "reference" —
it keeps running free-running AE, adapting to real floor lighting — and
manually lock the other cameras' exposure_us/analog_gain to match whatever
the reference converges to. It is a one-time calibration (not continuous
re-sync during live capture): calibrate once, Save, and the saved values are
applied to follower cameras whenever they enter hardware-trigger mode.

Gated behind experimental.exposure_sync_enabled (runtime_config) so it never
silently changes camera behaviour. All routes are prefixed with
/api/exposure-sync.
"""
from __future__ import annotations

import base64
import threading
import time

import runtime_config
import structlog
from camera.mindvision import CameraMode, MindVisionCamera
from flask import Blueprint, jsonify, request

from blueprints.mindvision import _apply_mv_settings, _encode_raw_frame, _read_mv_settings

logger = structlog.get_logger()

# Percentage step applied per nudge click (10%).
_NUDGE_FACTOR = 1.10


# ── Persisted state helpers ──────────────────────────────────────────────

def _get_enabled() -> bool:
    return bool(runtime_config.get("experimental.exposure_sync_enabled", False))


def _get_reference_cam() -> int | None:
    return runtime_config.get("experimental.exposure_sync_reference_cam", None)


def _get_saved_exposure_us() -> float | None:
    return runtime_config.get("experimental.exposure_sync_exposure_us", None)


def _get_saved_analog_gain() -> int | None:
    return runtime_config.get("experimental.exposure_sync_analog_gain", None)


# ── Hardware-trigger entry hook (called from app.py and blueprints/mindvision.py) ──

def apply_saved_state_if_enabled(cam: MindVisionCamera, cam_id: int) -> None:
    """Apply the saved follower exposure/gain when a non-reference camera enters
    hardware-trigger mode, if exposure sync is enabled and has been calibrated.

    Best-effort and silent: a failure here must never block the camera from
    entering hardware-trigger mode for actual production capture. This is the
    one-time-calibration model — it is called once per mode transition, not
    from the per-trigger capture hot path.
    """
    if not _get_enabled():
        return
    ref_id = _get_reference_cam()
    if ref_id is None or cam_id == ref_id:
        return
    exposure_us = _get_saved_exposure_us()
    analog_gain = _get_saved_analog_gain()
    if exposure_us is None or analog_gain is None:
        return
    if cam._h_camera is None:
        return
    try:
        applied, errors = _apply_mv_settings(cam._h_camera, {
            "ae_enabled": False,
            "exposure_us": exposure_us,
            "analog_gain": analog_gain,
        })
        if errors:
            logger.warning("exposure_sync_apply_on_trigger_entry_partial", camera_id=cam_id, errors=errors)
        else:
            logger.info(
                "exposure_sync_applied_on_trigger_entry",
                camera_id=cam_id, exposure_us=exposure_us, analog_gain=analog_gain,
            )
    except Exception:
        logger.exception("exposure_sync_apply_on_trigger_entry_failed", camera_id=cam_id)


def _grab_frame(cam: MindVisionCamera):
    """Grab one frame (soft-triggering if idle), same pattern as /settings/snapshot.

    Raises RuntimeError if no frame becomes available.
    """
    import mvsdk

    with cam._lock:
        if not cam._streaming and cam.mode != CameraMode.HARDWARE_TRIGGER:
            mvsdk.CameraSoftTrigger(cam._h_camera)
        frame, _head = cam._grab_frame(timeout_ms=cam.exposure_grab_timeout_ms())
    if frame is None:
        raise RuntimeError("No frame available — camera may still be exposing")
    return frame


def create_blueprint(
    cameras: dict[int, MindVisionCamera],
    serial_listener=None,
) -> Blueprint:
    bp = Blueprint("exposure_sync", __name__, url_prefix="/api/exposure-sync")

    def _hw_trigger_active() -> bool:
        if serial_listener is not None and getattr(serial_listener, "running", False):
            return True
        return any(cam.mode == CameraMode.HARDWARE_TRIGGER for cam in cameras.values())

    # ── State ────────────────────────────────────────────────────────────

    @bp.route("/state", methods=["GET"])
    def get_state():
        return jsonify({
            "enabled": _get_enabled(),
            "reference_camera_id": _get_reference_cam(),
            "exposure_us": _get_saved_exposure_us(),
            "analog_gain": _get_saved_analog_gain(),
            "calibrated_at": runtime_config.get("experimental.exposure_sync_calibrated_at", None),
            "camera_ids": sorted(cameras.keys()),
            "hw_trigger_active": _hw_trigger_active(),
        })

    @bp.route("/enabled", methods=["POST"])
    def set_enabled():
        body = request.get_json(silent=True) or {}
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be a boolean"}), 400

        if _hw_trigger_active():
            return jsonify({
                "error": (
                    "Cannot change exposure sync while the line is running in "
                    "hardware-trigger mode. Stop the line first "
                    "(POST /api/system/mode with mode='regular')."
                ),
            }), 409

        runtime_config.update("experimental.exposure_sync_enabled", enabled)

        if not enabled:
            ref_id = _get_reference_cam()
            for cam_id, cam in cameras.items():
                if cam_id == ref_id or cam._h_camera is None:
                    continue
                _apply_mv_settings(cam._h_camera, {"ae_enabled": True})

        logger.info("exposure_sync_enabled_changed", enabled=enabled)
        return jsonify({"enabled": enabled})

    # ── Calibration flow ─────────────────────────────────────────────────

    @bp.route("/reference", methods=["POST"])
    def set_reference():
        body = request.get_json(silent=True) or {}
        cam_id = body.get("camera_id")
        if not isinstance(cam_id, int) or isinstance(cam_id, bool) or cam_id not in cameras:
            return jsonify({"error": f"camera_id must be one of {sorted(cameras.keys())}"}), 400
        runtime_config.update("experimental.exposure_sync_reference_cam", cam_id)
        logger.info("exposure_sync_reference_set", camera_id=cam_id)
        return jsonify({"reference_camera_id": cam_id})

    @bp.route("/capture-reference", methods=["POST"])
    def capture_reference():
        """Grab a fresh AE-exposed frame on the reference camera and report what
        AE converged to. Does not return image bytes — the frontend fetches
        /api/cameras/settings/snapshot?camera_id=<ref> separately to display it.
        """
        ref_id = _get_reference_cam()
        if ref_id is None:
            return jsonify({"error": "No reference camera selected"}), 400
        cam = cameras.get(ref_id)
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Reference camera {ref_id} not found or not open"}), 404
        try:
            _grab_frame(cam)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 503

        s = _read_mv_settings(cam._h_camera, cam._cap)
        return jsonify({
            "camera_id": ref_id,
            "exposure_us": s["exposure_us"],
            "analog_gain": s["analog_gain"],
            "exposure_min_us": s["exposure_min_us"],
            "exposure_max_us": s["exposure_max_us"],
        })

    @bp.route("/nudge", methods=["POST"])
    def nudge():
        """Stateless: compute a new exposure_us from the client's current working
        value, nudged by a fixed percentage and clamped to the reference
        camera's supported range. No hardware write — call /apply to commit.
        """
        ref_id = _get_reference_cam()
        cam = cameras.get(ref_id) if ref_id is not None else None
        if cam is None or cam._h_camera is None:
            return jsonify({"error": "No reference camera selected or not open"}), 400

        body = request.get_json(silent=True) or {}
        try:
            current = float(body["current_exposure_us"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "current_exposure_us is required"}), 400
        direction = body.get("direction")
        if direction not in ("up", "down"):
            return jsonify({"error": "direction must be 'up' or 'down'"}), 400

        import mvsdk
        try:
            exp_min, exp_max, _ = mvsdk.CameraGetExposureTimeRange(cam._h_camera)
        except Exception:
            exp_min, exp_max = 26.0, 1_000_000.0

        factor = _NUDGE_FACTOR if direction == "up" else 1.0 / _NUDGE_FACTOR
        new_exposure = max(exp_min, min(exp_max, current * factor))
        return jsonify({
            "exposure_us": new_exposure,
            "exposure_min_us": exp_min,
            "exposure_max_us": exp_max,
        })

    @bp.route("/apply", methods=["POST"])
    def apply_to_followers():
        """Write exposure_us/analog_gain to every non-reference camera's live SDK
        handle (AE off). In-memory only — does not call CameraSaveParameter;
        only /save persists durably.
        """
        ref_id = _get_reference_cam()
        if ref_id is None:
            return jsonify({"error": "No reference camera selected"}), 400
        body = request.get_json(silent=True) or {}
        try:
            exposure_us = float(body["exposure_us"])
            analog_gain = int(body["analog_gain"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "exposure_us and analog_gain are required"}), 400

        results: dict[int, dict] = {}
        for cam_id, cam in cameras.items():
            if cam_id == ref_id:
                continue
            if cam._h_camera is None:
                results[cam_id] = {"applied": [], "errors": {"camera": "not open"}}
                continue
            applied, errors = _apply_mv_settings(cam._h_camera, {
                "ae_enabled": False,
                "exposure_us": exposure_us,
                "analog_gain": analog_gain,
            })
            results[cam_id] = {"applied": applied, "errors": errors}

        any_errors = any(r["errors"] for r in results.values())
        logger.info(
            "exposure_sync_applied", reference_camera_id=ref_id,
            exposure_us=exposure_us, analog_gain=analog_gain,
            results={str(k): v for k, v in results.items()},
        )
        return jsonify({
            "reference_camera_id": ref_id,
            "exposure_us": exposure_us,
            "analog_gain": analog_gain,
            "results": {str(k): v for k, v in results.items()},
        }), (207 if any_errors else 200)

    @bp.route("/preview", methods=["POST"])
    def preview_all():
        """Grab one frame from every camera and return small JPEGs as base64 for
        a side-by-side comparison — the real verification step, since /nudge
        only ever shows the reference camera's own brightness.
        """
        results: dict[int, str] = {}
        errors: dict[int, str] = {}
        mu = threading.Lock()

        def grab_one(cam_id: int, cam: MindVisionCamera) -> None:
            try:
                if cam._h_camera is None:
                    raise RuntimeError("camera not open")
                frame = _grab_frame(cam)
                jpeg = _encode_raw_frame(frame, max_width=800, quality=85)
                with mu:
                    results[cam_id] = base64.b64encode(jpeg).decode("ascii")
            except Exception as exc:
                with mu:
                    errors[cam_id] = str(exc)

        cam_ids = list(cameras.keys())
        threads = [
            threading.Thread(target=grab_one, args=(cam_id, cameras[cam_id]), daemon=True)
            for cam_id in cam_ids
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        timed_out = [cam_id for cam_id, t in zip(cam_ids, threads) if t.is_alive()]
        if timed_out:
            logger.warning("exposure_sync_preview_timed_out", camera_ids=timed_out)
            return jsonify({
                "error": "Preview timed out waiting for camera(s)",
                "details": {str(k): "timed out after 15 s" for k in timed_out},
            }), 504

        status = 207 if errors else 200
        return jsonify({
            "images": {str(k): v for k, v in results.items()},
            "errors": {str(k): v for k, v in errors.items()},
        }), status

    @bp.route("/save", methods=["POST"])
    def save_calibration():
        """Persist the calibrated reference/exposure/gain to runtime_config and
        call CameraSaveParameter on every follower's SDK handle so the manual
        exposure survives a camera reopen even before the next hardware-trigger
        entry re-applies it.
        """
        body = request.get_json(silent=True) or {}
        ref_id = body.get("camera_id")
        try:
            exposure_us = float(body["exposure_us"])
            analog_gain = int(body["analog_gain"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "camera_id, exposure_us, and analog_gain are required"}), 400
        if not isinstance(ref_id, int) or isinstance(ref_id, bool) or ref_id not in cameras:
            return jsonify({"error": f"camera_id must be one of {sorted(cameras.keys())}"}), 400

        import mvsdk
        save_errors: dict[int, str] = {}
        for cam_id, cam in cameras.items():
            if cam_id == ref_id or cam._h_camera is None:
                continue
            try:
                mvsdk.CameraSaveParameter(cam._h_camera, 0)
            except Exception as exc:
                save_errors[cam_id] = str(exc)

        runtime_config.update("experimental.exposure_sync_reference_cam", ref_id)
        runtime_config.update("experimental.exposure_sync_exposure_us", exposure_us)
        runtime_config.update("experimental.exposure_sync_analog_gain", analog_gain)
        runtime_config.update("experimental.exposure_sync_calibrated_at", time.time())

        logger.info(
            "exposure_sync_calibration_saved", reference_camera_id=ref_id,
            exposure_us=exposure_us, analog_gain=analog_gain,
            save_errors={str(k): v for k, v in save_errors.items()},
        )
        status = 207 if save_errors else 200
        return jsonify({
            "reference_camera_id": ref_id,
            "exposure_us": exposure_us,
            "analog_gain": analog_gain,
            "save_errors": {str(k): v for k, v in save_errors.items()},
        }), status

    return bp
