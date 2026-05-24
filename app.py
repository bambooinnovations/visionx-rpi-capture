import atexit
import os
import shutil
import tempfile
import threading
from pathlib import Path

import structlog

import calibration
import config
from camera import create_camera
from camera.mindvision import MindVisionCamera
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

capture_lock = threading.Lock()


@app.route("/rpi/stream")
def stream():
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


@app.route("/rpi/camera/white-balance")
def get_white_balance():
    wb = calibration.load().get("white_balance")
    if wb is None:
        return jsonify({"calibrated": False})
    return jsonify({"calibrated": True, **wb})


@app.route("/rpi/camera/calibrate-wb", methods=["POST"])
def calibrate_wb():
    if not isinstance(camera, MindVisionCamera):
        return jsonify({"error": "White balance calibration is only supported for MindVision cameras"}), 400
    try:
        gains = camera.calibrate_white_balance()
        return jsonify(gains)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception:
        logger.exception("calibrate_wb_failed")
        return jsonify({"error": "Calibration failed"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
