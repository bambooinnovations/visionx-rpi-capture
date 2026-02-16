import os
import shutil
import tempfile
import threading
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()
from flask import Flask, after_this_request, jsonify, request, send_file
from flask_cors import CORS

from imageCapture import DEFAULT_IMAGE_SIZE, capture_image, init_camera
from log_config import configure_logging
from metrics import get_stats, init_db, record_capture
from tasks import CAPTURE_TMP_DIR, start_cleanup_task

configure_logging(env=os.environ.get("ENV", "dev"))
logger = structlog.get_logger()

app = Flask(__name__)
CORS(app)

start_cleanup_task()
init_db()
try:
    init_camera()
except RuntimeError as e:
    logger.warning("camera_init_skipped", reason=str(e))

capture_lock = threading.Lock()


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

        # Ensure target_resolution is a tuple[int, int] (width/height may be Optional[int])
        w = width if width is not None else DEFAULT_IMAGE_SIZE[0]
        h = height if height is not None else DEFAULT_IMAGE_SIZE[1]
        target_resolution = (w, h)

        CAPTURE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(tempfile.mkdtemp(dir=CAPTURE_TMP_DIR))
        try:
            image_path, capture_metrics = capture_image(
                resolution=target_resolution,
                output_folder=tmp_path,
            )
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

        logger.info("image_captured", width=w, height=h, file=image_path.name)
        return send_file(image_path)
    finally:
        capture_lock.release()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
