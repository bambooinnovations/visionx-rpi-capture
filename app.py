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

from flask import Flask, Response, after_this_request, jsonify, request, send_file
from flask_cors import CORS

configure_logging(env=config.ENV)
logger = structlog.get_logger()

app = Flask(__name__)
CORS(app)

start_cleanup_task()
init_db()
camera = create_camera()
# In Flask debug mode the stat reloader forks a child process; both the parent
# and the child would hit CameraInit, causing the second to fail with -18
# ("device already open"). Only open in the child (WERKZEUG_RUN_MAIN=true) or
# when not in debug mode at all.
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    try:
        camera.open()
    except RuntimeError as e:
        logger.warning("camera_init_skipped", reason=str(e))
    atexit.register(camera.close)

if isinstance(camera, MindVisionCamera):
    from blueprints.mindvision import create_blueprint
    app.register_blueprint(create_blueprint(camera))

capture_lock = threading.Lock()


@app.route("/rpi/stream")
def stream():
    if isinstance(camera, MindVisionCamera) and camera.mode != CameraMode.STREAM:
        return Response("Camera is not in stream mode", status=409)

    def generate():
        for frame in camera.stream_frames():
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


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
    if isinstance(camera, MindVisionCamera) and camera.mode != CameraMode.CAPTURE:
        return jsonify({"error": "Camera is not in capture mode"}), 409

    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "Capture already in progress"}), 429
    try:
        width = request.args.get("width", type=int)
        height = request.args.get("height", type=int)

        if (width is None) != (height is None):
            return jsonify({"error": "Provide both width and height, or neither"}), 400

        if width is not None and (width <= 0 or height <= 0):
            return jsonify({"error": "width and height must be positive integers"}), 400

        # None → capture_image() uses the profile / auto-detected resolution.
        target_resolution = (width, height) if width is not None else None

        CAPTURE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(tempfile.mkdtemp(dir=CAPTURE_TMP_DIR))
        try:
            image_path, capture_metrics = camera.capture_image(
                resolution=target_resolution,
                output_folder=tmp_path,
            )
        except RuntimeError as e:
            shutil.rmtree(tmp_path, ignore_errors=True)
            logger.warning("capture_no_camera", reason=str(e))
            return jsonify({"error": "No camera detected"}), 503
        except Exception:
            shutil.rmtree(tmp_path, ignore_errors=True)
            logger.exception("capture_failed")
            return jsonify({"error": "Capture failed"}), 500

        try:
            record_capture(capture_metrics)
        except Exception:
            logger.exception("record_metrics_failed")

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmp_path, ignore_errors=True)
            return response

        logger.info("image_captured", resolution=target_resolution, file=image_path.name)
        return send_file(image_path)
    finally:
        capture_lock.release()


def _effective_config() -> dict:
    """Merge toml base values with runtime overrides. Masks sensitive keys."""
    base = {
        "camera.mv_exposure_us":          config.MV_EXPOSURE_US,
        "camera.mv_auto_exposure":        config.MV_AUTO_EXPOSURE,
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
        if isinstance(camera, MindVisionCamera):
            camera.apply_config(key, value)

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
