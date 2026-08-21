import atexit
import os
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests as _requests
import structlog

import config
import runtime_config
from camera import build_camera_registry
from camera.mindvision import CameraMode, MindVisionCamera
from log_config import configure_logging
from metrics import get_stats, init_db, record_capture
from tasks import CAPTURE_TMP_DIR, start_cleanup_task

from flask import Flask, Response, after_this_request, jsonify, redirect, request, send_file, render_template
from flask_cors import CORS

configure_logging(env=config.ENV, log_dir=Path(__file__).parent / "logs")
logger = structlog.get_logger()

def _startup_info() -> dict:
    import platform, subprocess
    info: dict = {"env": config.ENV}
    try:
        import cv2, numpy as np
        info["opencv"] = cv2.__version__
        info["numpy"] = np.__version__
    except ImportError:
        pass
    info["python"] = platform.python_version()
    info["platform"] = f"{platform.machine()} {platform.release()}"
    try:
        with open("/proc/cpuinfo") as _f:
            for _line in _f:
                if _line.startswith("Model"):
                    info["board"] = _line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        _r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2)
        info["throttled"] = _r.stdout.strip()
    except Exception:
        pass
    return info

logger.info("app_startup", **_startup_info())

app = Flask(__name__)
CORS(app)

start_cleanup_task()
init_db()

# Build camera registry — see camera.build_camera_registry() for the
# auto-detect / mixed-type rules. MindVision-only features (hardware trigger,
# mode switching, stitching) key off mindvision_cameras, never the full
# cameras dict, so a non-MindVision camera in a mixed registry is left alone.
cameras: dict[int, object] = build_camera_registry()
mindvision_cameras: dict[int, MindVisionCamera] = {
    cam_id: cam for cam_id, cam in cameras.items() if isinstance(cam, MindVisionCamera)
}

# Per-camera locks so concurrent single-camera captures don't block each other.
capture_locks: dict[int, threading.Lock] = {
    cam_id: threading.Lock() for cam_id in cameras
}

# In Werkzeug debug/reload mode the reloader parent forks a child
# (WERKZEUG_RUN_MAIN=true) that actually serves requests. Only that child
# should open cameras — the parent must not, or it wins the CameraInit race
# and leaves the child with no camera.
# app.debug is always False at module-import time when using `flask run --debug`
# because Flask sets it after the CLI parses flags, so we check FLASK_DEBUG
# from the environment instead.
_debug_mode = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true")
if not _debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    for cam_id, cam in cameras.items():
        try:
            cam.open()
        except RuntimeError as e:
            logger.warning("camera_init_skipped", camera_id=cam_id, reason=str(e))
        atexit.register(cam.close)

    logger.info(
        "cameras_ready",
        count=len(cameras),
        cameras=[
            {
                "camera_id": cam_id,
                "model": cam.camera_info().get("model"),
                "serial_number": cam.camera_info().get("serial_number"),
                "status": "open" if cam.camera_info().get("status") != "closed" else "closed",
            }
            for cam_id, cam in sorted(cameras.items())
        ],
    )

if mindvision_cameras:
    from camera.mindvision_trigger import SerialTriggerListener, seed_default_speed_presets
    from blueprints.stitch import _load_calibration as _stitch_load_cal, _stitch_frames
    _serial_listener = SerialTriggerListener(
        mindvision_cameras,
        load_calibration=_stitch_load_cal,
        stitch_frames=_stitch_frames,
    )
    seed_default_speed_presets()

    from blueprints.mindvision import create_blueprint
    from blueprints.stitch import create_blueprint as create_stitch_blueprint
    from blueprints.lens import create_blueprint as create_lens_blueprint
    from blueprints.arduino import create_blueprint as create_arduino_blueprint
    from blueprints.debug_capture import create_blueprint as create_debug_capture_blueprint
    from blueprints.exposure_sync import (
        create_blueprint as create_exposure_sync_blueprint,
        apply_saved_state_if_enabled,
    )
    app.register_blueprint(create_blueprint(mindvision_cameras, _serial_listener))
    app.register_blueprint(create_stitch_blueprint(mindvision_cameras))
    app.register_blueprint(create_lens_blueprint(mindvision_cameras))
    app.register_blueprint(create_arduino_blueprint(_serial_listener, mindvision_cameras))
    app.register_blueprint(create_debug_capture_blueprint(mindvision_cameras))
    app.register_blueprint(create_exposure_sync_blueprint(mindvision_cameras, _serial_listener))

    if (not _debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        if config.is_capture_station():
            # QC/shading station: boot cameras straight into software-trigger
            # capture mode so POST /rpi/capture works immediately with no
            # manual mode switch. The decoder never auto-starts on these
            # stations.
            _mode_errors = {}
            for _cam_id, _cam in mindvision_cameras.items():
                try:
                    _cam.set_mode(CameraMode.CAPTURE)
                except Exception as _e:
                    _mode_errors[_cam_id] = str(_e)
            if _mode_errors:
                logger.warning("qc_capture_mode_failed", mode_errors=_mode_errors)
            else:
                logger.info("qc_station_ready", camera_ids=sorted(mindvision_cameras.keys()))
        else:
            # Fabric station (default): auto-start decoder if the Arduino serial
            # port is already present at boot.
            if os.path.exists(config.HW_TRIGGER_SERIAL_PORT):
                _mode_errors = {}
                for _cam_id, _cam in mindvision_cameras.items():
                    try:
                        _cam.set_mode(CameraMode.HARDWARE_TRIGGER)
                    except Exception as _e:
                        _mode_errors[_cam_id] = str(_e)
                    else:
                        apply_saved_state_if_enabled(_cam, _cam_id)
                if not _mode_errors:
                    try:
                        _serial_listener.start(
                            port=config.HW_TRIGGER_SERIAL_PORT,
                            baud=config.HW_TRIGGER_SERIAL_BAUD,
                        )
                        logger.info("decoder_auto_started", port=config.HW_TRIGGER_SERIAL_PORT)
                    except Exception as _e:
                        logger.warning("decoder_auto_start_failed", error=str(_e))
                else:
                    logger.warning("decoder_auto_start_skipped", mode_errors=_mode_errors)
else:
    # No MindVision camera in the registry, so none of the routes above were
    # registered. Without this, any request under these prefixes falls through
    # to Flask's default 404 — an HTML page, not JSON — which every caller
    # (dashboard, monitor page, curl) just sees as an opaque "HTTP 404" with
    # no explanation. Catch every path under these prefixes and answer with a
    # clear, parseable reason instead.
    _MV_UNAVAILABLE_MSG = (
        "No MindVision camera detected on this device — hardware trigger, "
        "stitching, and lens control are unavailable until one is connected."
    )

    def _mindvision_unavailable(**_kwargs):
        return jsonify({"error": _MV_UNAVAILABLE_MSG, "mindvision_available": False}), 503

    for _prefix in ("/api/decoder", "/api/cameras", "/api/stitch", "/api/lens"):
        _endpoint = _prefix.strip("/").replace("/", "_")
        app.add_url_rule(
            _prefix, endpoint=f"{_endpoint}_unavailable", view_func=_mindvision_unavailable,
            methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
        )
        app.add_url_rule(
            f"{_prefix}/<path:_sub>", endpoint=f"{_endpoint}_sub_unavailable", view_func=_mindvision_unavailable,
            methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
        )

from blueprints.pxcm import create_blueprint as create_pxcm_blueprint
app.register_blueprint(create_pxcm_blueprint(cameras))


def _resolve_camera(default_id: int = 0):
    """Return (cam, cam_id) from the camera_id query param, or None on miss."""
    cam_id = request.args.get("camera_id", default_id, type=int)
    return cameras.get(cam_id), cam_id


@app.route("/api/system/cameras")
def system_cameras():
    """Every camera in the registry, any type — for UI camera discovery.

    Unlike GET /api/cameras (MindVision-only, under blueprints/mindvision.py),
    this always exists and includes the "type" field so the frontend can
    branch between full MindVision tooling and a plain view-only camera.
    """
    result = []
    for cam_id, cam in sorted(cameras.items()):
        info = cam.camera_info()
        result.append({
            "camera_id": cam_id,
            "type": info.get("type"),
            "model": info.get("model"),
            "status": "open" if info.get("status") != "closed" else "closed",
            "serial_number": info.get("serial_number"),
            "product_name": info.get("product_name"),
            "port_type": info.get("port_type"),
            "capture_size": info.get("capture_size"),
            "stream_size": info.get("stream_size"),
        })
    return jsonify(result)


@app.route("/rpi/stream")
def stream():
    cam, cam_id = _resolve_camera()
    if cam is None:
        return Response(f"Camera {cam_id} not found", status=404)

    if isinstance(cam, MindVisionCamera) and cam.mode == CameraMode.HARDWARE_TRIGGER:
        return Response("Camera is in hardware trigger mode", status=409)

    width = request.args.get("width", None, type=int)
    height = request.args.get("height", None, type=int)
    fps = request.args.get("fps", None, type=float)

    if (width is not None and width <= 0) or (height is not None and height <= 0):
        return jsonify({"error": "width and height must be positive integers"}), 400

    if isinstance(cam, MindVisionCamera):
        if not cam._stream_lock.acquire(blocking=False):
            # Lock is held — signal the active stream to exit cleanly, then
            # wait for it to release the lock. The old stream will stop within
            # one frame interval once it sees the cancel event.
            cam._stream_cancel.set()
            if not cam._stream_lock.acquire(timeout=5):
                return jsonify({"error": f"Camera {cam_id} stream already in use"}), 409

    def generate():
        try:
            stream_kwargs = {}
            if width is not None:
                stream_kwargs["width"] = width
            if height is not None:
                stream_kwargs["height"] = height
            if fps is not None:
                stream_kwargs["fps"] = fps
            for frame in cam.stream_frames(**stream_kwargs):
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
        finally:
            if isinstance(cam, MindVisionCamera):
                cam._stream_lock.release()

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/")
def portal_home():
    return render_template("index.html")


@app.route("/calibrate")
def calibrate_ui():
    return render_template("calibrate.html")


@app.route("/stitch")
def stitch_ui():
    return render_template("stitch.html")


@app.route("/focus")
def focus_ui():
    return render_template("focus.html")


@app.route("/pxcm")
def pxcm_ui():
    return render_template("pxcm.html")


@app.route("/settings")
def system_settings_ui():
    return render_template("system_settings.html")


@app.route("/monitor")
def monitor_ui():
    return render_template("monitor.html")


@app.route("/debug-capture")
def debug_capture_ui():
    if not mindvision_cameras:
        return redirect("/")
    return render_template("debug_capture.html")


@app.route("/exposure-sync")
def exposure_sync_ui():
    if not mindvision_cameras:
        return redirect("/")
    return render_template("exposure_sync.html")


@app.route("/mindvision/<int:camera_id>/settings")
def mindvision_settings_page(camera_id):
    if not isinstance(cameras.get(camera_id), MindVisionCamera):
        return redirect("/")
    return render_template("mindvision_settings.html", camera_id=camera_id)


@app.route("/api/health")
def health():
    return jsonify({"status": "healthy"})


def _build_system_status() -> tuple[bool, dict]:
    """Return (ready, subsystems) reflecting the current state of all subsystems."""
    # --- cameras ---
    cam_list = []
    for cam_id, cam in sorted(cameras.items()):
        info = cam.camera_info()
        entry = {
            "id": cam_id,
            "open": info.get("status") != "closed",
            "serial": info.get("serial_number"),
        }
        if isinstance(cam, MindVisionCamera):
            entry["mode"] = cam.mode.value
        cam_list.append(entry)

    # QC/shading stations run cameras in software-trigger capture mode; fabric
    # stations expect hardware-trigger mode. Readiness must reflect whichever
    # applies.
    _expected_mode = (
        CameraMode.CAPTURE.value if config.is_capture_station()
        else CameraMode.HARDWARE_TRIGGER.value
    )
    cameras_ready = len(cam_list) > 0 and all(c["open"] for c in cam_list)
    if mindvision_cameras:
        cameras_ready = cameras_ready and all(
            c.get("mode") == _expected_mode for c in cam_list if "mode" in c
        )
    cameras_subsystem = {"ready": cameras_ready, "cameras": cam_list}

    # --- decoder (fabric station, MindVision only — QC/shading stations have no decoder) ---
    decoder_subsystem = None
    if mindvision_cameras and not config.is_capture_station():
        s = _serial_listener.status()
        running = s.get("running", False)
        serial_connected = s.get("serial_connected", False)
        trigger_enabled = s.get("trigger_enabled", False)
        decoder_subsystem = {
            "ready": running and serial_connected and trigger_enabled,
            "running": running,
            "serial_connected": serial_connected,
            "trigger_enabled": trigger_enabled,
        }

    # --- config ---
    # Destination URL only matters for the fabric station's hardware-trigger
    # upload pipeline; QC/shading stations capture on demand and don't need
    # it set.
    if config.is_capture_station():
        config_subsystem = {"ready": True, "destination_url": ""}
    else:
        destination_url = runtime_config.get(
            "hw_trigger.destination_url", config.HW_TRIGGER_DESTINATION_URL
        )
        config_subsystem = {
            "ready": bool(destination_url),
            "destination_url": destination_url or "",
        }

    # --- stitching (multi-camera MindVision only) ---
    stitching_subsystem = None
    if len(mindvision_cameras) > 1:
        from blueprints.stitch import _load_calibration
        cal = _load_calibration()
        cal_camera_keys = list(cal.get("cameras", {}).keys()) if cal else []
        active_ids = [str(i) for i in sorted(mindvision_cameras.keys())]
        stitching_subsystem = {
            "ready": cal is not None and all(k in cal_camera_keys for k in active_ids),
            "calibrated_cameras": [int(k) for k in cal_camera_keys],
        }

    # stitching not required — falls back to single camera when uncalibrated
    ready = cameras_ready and config_subsystem["ready"]
    if decoder_subsystem is not None:
        ready = ready and decoder_subsystem["ready"]

    subsystems = {"cameras": cameras_subsystem, "config": config_subsystem}
    if decoder_subsystem is not None:
        subsystems["decoder"] = decoder_subsystem
    if stitching_subsystem is not None:
        subsystems["stitching"] = stitching_subsystem

    return ready, subsystems


@app.route("/api/system/ready", methods=["GET"])
def system_ready():
    ready, subsystems = _build_system_status()
    return jsonify({"ready": ready, "subsystems": subsystems})


@app.route("/api/system/mode", methods=["POST"])
def system_mode():
    """Switch the system between high-level operating modes.

    Body: {"mode": "fabric" | "regular"}

    fabric  — hardware trigger + stitching pipeline (production capture).
              Opens cameras, sets hardware trigger mode, starts the decoder.
    regular — calibration / software trigger workflow.
              Stops the decoder and reverts cameras to capture mode.
    """
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode not in ("fabric", "regular"):
        return jsonify({"error": "mode must be 'fabric' or 'regular'"}), 400

    if mode == "fabric" and config.is_capture_station():
        return jsonify({"error": "fabric mode is not available on a qc/shading station"}), 409

    actions: list[dict] = []

    if mindvision_cameras:
        if mode == "fabric":
            for cam_id, cam in sorted(mindvision_cameras.items()):
                if cam.camera_info().get("status") == "closed":
                    try:
                        cam.open()
                        actions.append({"action": "open_camera", "camera_id": cam_id, "ok": True})
                    except Exception as exc:
                        actions.append({"action": "open_camera", "camera_id": cam_id, "ok": False, "error": str(exc)})

            for cam_id, cam in sorted(mindvision_cameras.items()):
                if cam.mode != CameraMode.HARDWARE_TRIGGER:
                    try:
                        cam.set_mode(CameraMode.HARDWARE_TRIGGER)
                        actions.append({"action": "set_hardware_trigger_mode", "camera_id": cam_id, "ok": True})
                    except Exception as exc:
                        actions.append({"action": "set_hardware_trigger_mode", "camera_id": cam_id, "ok": False, "error": str(exc)})
                    else:
                        apply_saved_state_if_enabled(cam, cam_id)

            if not _serial_listener.running:
                try:
                    _serial_listener.start(port=config.HW_TRIGGER_SERIAL_PORT, baud=config.HW_TRIGGER_SERIAL_BAUD)
                    actions.append({"action": "start_decoder", "ok": True})
                except Exception as exc:
                    actions.append({"action": "start_decoder", "ok": False, "error": str(exc)})

        elif mode == "regular":
            if _serial_listener.running:
                try:
                    _serial_listener.stop()
                    actions.append({"action": "stop_decoder", "ok": True})
                except Exception as exc:
                    actions.append({"action": "stop_decoder", "ok": False, "error": str(exc)})

            for cam_id, cam in sorted(mindvision_cameras.items()):
                if cam.mode != CameraMode.CAPTURE:
                    try:
                        cam.set_mode(CameraMode.CAPTURE)
                        actions.append({"action": "set_capture_mode", "camera_id": cam_id, "ok": True})
                    except Exception as exc:
                        actions.append({"action": "set_capture_mode", "camera_id": cam_id, "ok": False, "error": str(exc)})

    ready, subsystems = _build_system_status()
    return jsonify({"mode": mode, "actions": actions, "ready": ready, "subsystems": subsystems})


@app.route("/api/metrics/stats")
def metrics_stats():
    try:
        return jsonify(get_stats())
    except Exception:
        logger.exception("metrics_stats_failed")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/rpi/capture", methods=["POST"])
def capture():
    cam, cam_id = _resolve_camera()
    if cam is None:
        return jsonify({"error": f"Camera {cam_id} not found"}), 404

    if isinstance(cam, MindVisionCamera) and cam.mode == CameraMode.HARDWARE_TRIGGER:
        return jsonify({"error": "Camera is in hardware trigger mode; use hardware signal to capture"}), 409

    lock = capture_locks[cam_id]
    if not lock.acquire(blocking=False):
        return jsonify({"error": "Capture already in progress"}), 429
    try:
        width = request.args.get("width", type=int)
        height = request.args.get("height", type=int)

        if (width is None) != (height is None):
            return jsonify({"error": "Provide both width and height, or neither"}), 400

        if width is not None and (width <= 0 or height <= 0):
            return jsonify({"error": "width and height must be positive integers"}), 400

        target_resolution = (width, height) if width is not None else None

        CAPTURE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(tempfile.mkdtemp(dir=CAPTURE_TMP_DIR))
        try:
            image_path, capture_metrics = cam.capture_image(
                resolution=target_resolution,
                output_folder=tmp_path,
            )
        except RuntimeError as e:
            shutil.rmtree(tmp_path, ignore_errors=True)
            logger.warning("capture_no_camera", camera_id=cam_id, reason=str(e))
            return jsonify({"error": "No camera detected"}), 503
        except Exception:
            shutil.rmtree(tmp_path, ignore_errors=True)
            logger.exception("capture_failed", camera_id=cam_id)
            return jsonify({"error": "Capture failed"}), 500

        try:
            record_capture(capture_metrics)
        except Exception:
            logger.exception("record_metrics_failed")

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmp_path, ignore_errors=True)
            return response

        logger.info("image_captured", camera_id=cam_id, resolution=target_resolution, file=image_path.name)
        return send_file(image_path)
    finally:
        lock.release()


def _effective_config() -> dict:
    """Merge toml base values with runtime overrides. Masks sensitive keys."""
    base = {
        "station.type":                   config.STATION_TYPE,
        "stream.fps":                     config.STREAM_FPS,
        "stream.quality":                 config.STREAM_QUALITY,
        "hw_trigger.serial_port":         config.HW_TRIGGER_SERIAL_PORT,
        "hw_trigger.serial_baud":         config.HW_TRIGGER_SERIAL_BAUD,
        "hw_trigger.destination_url":     config.HW_TRIGGER_DESTINATION_URL,
        "hw_trigger.destination_api_key": "***" if config.HW_TRIGGER_DESTINATION_API_KEY else "",
        "hw_trigger.retry_attempts":      config.HW_TRIGGER_RETRY_ATTEMPTS,
        "hw_trigger.timeout_seconds":     config.HW_TRIGGER_TIMEOUT_SECONDS,
        "hw_trigger.save_local":          config.HW_TRIGGER_SAVE_LOCAL,
        "hw_trigger.local_save_dir":      str(config.HW_TRIGGER_LOCAL_SAVE_DIR),
        "hw_trigger.local_max_files":     config.HW_TRIGGER_LOCAL_MAX_FILES,
        "hw_trigger.local_max_mb":        config.HW_TRIGGER_LOCAL_MAX_MB,
        "hw_trigger.raw_destination_url": config.HW_TRIGGER_RAW_DESTINATION_URL,
        "hw_trigger.send_raw_images":     config.HW_TRIGGER_SEND_RAW_IMAGES,
    }
    overrides = runtime_config.load()
    for key, value in overrides.items():
        if key in runtime_config.UPDATABLE:
            base[key] = "***" if key in runtime_config.MASKED_KEYS and value else value
    return base


@app.route("/api/system/config", methods=["GET"])
def get_config():
    overrides = runtime_config.load()
    return jsonify({
        "config": _effective_config(),
        "runtime_overrides": list(overrides.keys()),
    })


@app.route("/api/system/config", methods=["PATCH"])
def patch_config():
    body = request.get_json(silent=True) or {}
    if not body:
        return jsonify({"error": "Request body must be a JSON object"}), 400

    errors = {}
    updates = {}
    for key, value in body.items():
        if key not in runtime_config.UPDATABLE:
            errors[key] = f"not updatable at runtime"
            continue
        expected = runtime_config.UPDATABLE[key]
        try:
            if expected is bool:
                if not isinstance(value, bool):
                    raise ValueError("expected boolean")
                coerced = value
            else:
                coerced = expected(value)
        except (ValueError, TypeError):
            errors[key] = f"expected {expected.__name__}"
            continue
        updates[key] = coerced

    if errors:
        return jsonify({"error": "invalid values", "details": errors}), 400

    for key, value in updates.items():
        runtime_config.update(key, value)
        for cam in cameras.values():
            if isinstance(cam, MindVisionCamera):
                cam.apply_config(key, value)

    logger.info("runtime_config_updated", keys=list(updates.keys()))
    return jsonify({"updated": list(updates.keys()), "config": _effective_config()})


@app.route("/api/system/check-url", methods=["GET"])
def check_url():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url parameter required"}), 400
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return jsonify({"ok": False, "error": "Invalid URL — must use http:// or https://"}), 200
        health_url = f"{parsed.scheme}://{parsed.netloc}/health"
        r = _requests.get(health_url, timeout=5)
        try:
            data = r.json()
        except ValueError:
            return jsonify({"ok": False, "error": "Server responded but returned a non-JSON body"})
        if data.get("status") in ("ok", "healthy"):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Server responded but health check failed"})
    except _requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": "Could not connect to server"})
    except _requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "Connection timed out"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/system/config/<string:key>", methods=["DELETE"])
def delete_config(key: str):
    if key not in runtime_config.UPDATABLE:
        return jsonify({"error": f"'{key}' is not a runtime-updatable key"}), 400
    existed = runtime_config.delete(key)
    if not existed:
        return jsonify({"error": f"No runtime override set for '{key}'"}), 404
    logger.info("runtime_config_deleted", key=key)
    return jsonify({"deleted": key, "config": _effective_config()})


if __name__ == "__main__":
    # threaded=True: Werkzeug's dev server is single-request by default, which
    # deadlocks the whole app against any long-lived connection — in particular
    # the /api/decoder/queues/stream SSE endpoint (used by the dashboard's Dev
    # Mode and the /monitor page). Without this, one open SSE stream blocks
    # every other request until it times out, surfacing as "Failed to fetch".
    # Production already runs under gunicorn with multiple threads, so this
    # only matters for `python app.py` / the Flask dev server.
    app.run(host="0.0.0.0", port=8080, threaded=True)
