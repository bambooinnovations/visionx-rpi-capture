import atexit
import os
import shutil
import tempfile
import threading
from pathlib import Path

import structlog

import config
import runtime_config
from camera import create_camera
from camera.mindvision import CameraMode, MindVisionCamera
from log_config import configure_logging
from metrics import get_stats, init_db, record_capture
from tasks import CAPTURE_TMP_DIR, start_cleanup_task

from flask import Flask, Response, after_this_request, jsonify, redirect, request, send_file, render_template
from flask_cors import CORS

configure_logging(env=config.ENV)
logger = structlog.get_logger()

app = Flask(__name__)
CORS(app)

start_cleanup_task()
init_db()

# Build camera registry.
# For MindVision: enumerate all connected devices at startup — every camera
# that's plugged in gets an instance. For all other types: single-camera factory.
if config.CAMERA_TYPE == "mindvision":
    _count = 0
    try:
        import mvsdk as _mvsdk
        import camera.mindvision as _mv_mod

        _mvsdk.CameraSdkInit(0)  # must be called before any other SDK function (0 = English)
        _mvsdk.CameraSetDataDirectory(str(Path(__file__).parent / "MindVisionCamera"))
        # Mark the SDK as initialized and seed the device list cache so that
        # MindVisionCamera.open() does not call CameraSdkInit a second time and
        # does not re-enumerate (which may return a different order or omit
        # already-initialized cameras).
        _mv_mod._sdk_initialized = True
        _dev_list = _mvsdk.CameraEnumerateDevice()
        _mv_mod._dev_list_cache = _dev_list
        _count = len(_dev_list)
        logger.info("mindvision_cameras_detected", count=_count)
    except Exception as _e:
        logger.warning("mindvision_enumerate_failed", reason=str(_e))
    if _count == 0:
        _count = 1  # create one anyway so open() surfaces a clear error

    cameras: dict[int, object] = {
        i: MindVisionCamera(camera_index=i) for i in range(_count)
    }
else:
    cameras = {0: create_camera()}

# Convenience reference to camera 0 — used for isinstance checks that apply
# to all cameras of the same type.
camera = cameras[0]

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

if isinstance(camera, MindVisionCamera):
    from blueprints.mindvision import create_blueprint
    from blueprints.stitch import create_blueprint as create_stitch_blueprint
    from blueprints.lens import create_blueprint as create_lens_blueprint
    app.register_blueprint(create_blueprint(cameras))
    app.register_blueprint(create_stitch_blueprint(cameras))
    app.register_blueprint(create_lens_blueprint(cameras))


def _resolve_camera(default_id: int = 0):
    """Return (cam, cam_id) from the camera_id query param, or None on miss."""
    cam_id = request.args.get("camera_id", default_id, type=int)
    return cameras.get(cam_id), cam_id


@app.route("/rpi/stream")
def stream():
    cam, cam_id = _resolve_camera()
    if cam is None:
        return Response(f"Camera {cam_id} not found", status=404)

    if isinstance(cam, MindVisionCamera) and cam.mode != CameraMode.STREAM:
        return Response("Camera is not in stream mode", status=409)

    width = request.args.get("width", None, type=int)
    height = request.args.get("height", None, type=int)
    fps = request.args.get("fps", None, type=float)

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
            if isinstance(cam, MindVisionCamera):
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


@app.route("/mindvision/<int:camera_id>/settings")
def mindvision_settings_page(camera_id):
    if not isinstance(camera, MindVisionCamera):
        return redirect("/")
    if cameras.get(camera_id) is None:
        return redirect("/")
    return render_template("mindvision_settings.html", camera_id=camera_id)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/metrics/stats")
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
        "stream.fps":                     config.STREAM_FPS,
        "stream.quality":                 config.STREAM_QUALITY,
        "hw_trigger.destination_url":     config.HW_TRIGGER_DESTINATION_URL,
        "hw_trigger.destination_api_key": "***" if config.HW_TRIGGER_DESTINATION_API_KEY else "",
        "hw_trigger.retry_attempts":      config.HW_TRIGGER_RETRY_ATTEMPTS,
        "hw_trigger.timeout_seconds":     config.HW_TRIGGER_TIMEOUT_SECONDS,
        "hw_trigger.save_local":          config.HW_TRIGGER_SAVE_LOCAL,
        "hw_trigger.local_max_files":     config.HW_TRIGGER_LOCAL_MAX_FILES,
        "hw_trigger.local_max_mb":        config.HW_TRIGGER_LOCAL_MAX_MB,
    }
    overrides = runtime_config.load()
    for key, value in overrides.items():
        if key in runtime_config.UPDATABLE:
            base[key] = "***" if key in runtime_config.MASKED_KEYS and value else value
    return base


@app.route("/rpi/config", methods=["GET"])
def get_config():
    overrides = runtime_config.load()
    return jsonify({
        "config": _effective_config(),
        "runtime_overrides": list(overrides.keys()),
    })


@app.route("/rpi/config", methods=["PATCH"])
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


@app.route("/rpi/config/<string:key>", methods=["DELETE"])
def delete_config(key: str):
    if key not in runtime_config.UPDATABLE:
        return jsonify({"error": f"'{key}' is not a runtime-updatable key"}), 400
    existed = runtime_config.delete(key)
    if not existed:
        return jsonify({"error": f"No runtime override set for '{key}'"}), 404
    logger.info("runtime_config_deleted", key=key)
    return jsonify({"deleted": key, "config": _effective_config()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
