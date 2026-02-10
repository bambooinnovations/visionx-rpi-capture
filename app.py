import logging
import random
import threading
from pathlib import Path

from flask import Flask, jsonify, send_file
from flask_cors import CORS

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

capture_lock = threading.Lock()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/rpi/capture", methods=["POST"])
def capture():
    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "Capture already in progress"}), 429
    try:
        images = [
            f.name for f in STATIC_DIR.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if not images:
            return jsonify({"error": "No images available"}), 503
        chosen = random.choice(images)
        logger.info("Serving image: %s", chosen)
        return send_file(STATIC_DIR / chosen)
    finally:
        capture_lock.release()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
