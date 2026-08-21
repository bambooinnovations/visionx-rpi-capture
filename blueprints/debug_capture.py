"""Manual multi-camera debug capture blueprint.

Lets an operator grab a still from any subset of MindVision cameras on
demand (via software trigger — hardware-trigger mode has nothing to fire a
capture on demand, see camera/mindvision.py capture_image()), review it, and
forward it to a dedicated debug destination, separate from production
hw_trigger uploads. All routes are prefixed with /api/debug.
"""
from __future__ import annotations

import base64
import shutil
import tempfile
import time
from pathlib import Path

import config
import runtime_config
import structlog
from camera.mindvision import CameraMode, MindVisionCamera, capture_many
from flask import Blueprint, jsonify, request

logger = structlog.get_logger()


def _get_destination_url() -> str:
    return runtime_config.get("debug.destination_url", config.DEBUG_DESTINATION_URL)


def _get_api_key() -> str:
    return runtime_config.get("debug.destination_api_key", config.DEBUG_DESTINATION_API_KEY)


def _post_debug_image(jpeg_bytes: bytes, filename: str, camera_id: int, captured_at: str) -> tuple[bool, str | None]:
    """Single-attempt POST to the debug destination. No retry — this is an
    interactive action, the operator can just click Upload again on failure.
    """
    import io
    import requests

    url = _get_destination_url()
    if not url:
        return False, "No debug destination configured"

    headers = {}
    api_key = _get_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = requests.post(
            url,
            files={"image": (filename, io.BytesIO(jpeg_bytes), "image/jpeg")},
            data={"camera_id": str(camera_id), "captured_at": captured_at, "source": "debug"},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("debug_upload_ok", url=url, filename=filename, camera_id=camera_id, status=resp.status_code)
        return True, None
    except Exception as exc:
        logger.warning("debug_upload_failed", url=url, filename=filename, camera_id=camera_id, error=str(exc))
        return False, str(exc)


def create_blueprint(cameras: dict[int, MindVisionCamera]) -> Blueprint:
    bp = Blueprint("debug_capture", __name__, url_prefix="/api/debug")

    # ── State ────────────────────────────────────────────────────────────

    @bp.route("/state", methods=["GET"])
    def get_state():
        return jsonify({
            "camera_ids": sorted(cameras.keys()),
            "cameras": {
                str(cam_id): {"mode": cam.mode.value}
                for cam_id, cam in cameras.items()
            },
            "destination_configured": bool(_get_destination_url()),
        })

    # ── Capture ──────────────────────────────────────────────────────────

    @bp.route("/capture", methods=["POST"])
    def capture():
        """Software-trigger a still from each requested camera and return it as
        base64 JPEG (EXIF already embedded, via the same cam.capture_image()
        production uses). Never contacts the external destination — this is
        purely "take the photo and show me"; /upload is a separate step.
        """
        body = request.get_json(silent=True) or {}
        requested_raw = body.get("camera_ids")
        if not isinstance(requested_raw, list) or not requested_raw:
            return jsonify({"error": "camera_ids must be a non-empty list"}), 400
        requested = list(dict.fromkeys(requested_raw))

        unknown = [cid for cid in requested if cid not in cameras]
        if unknown:
            return jsonify({"error": f"Unknown camera_id(s): {unknown}"}), 404

        blocked = [cid for cid in requested if cameras[cid].mode == CameraMode.HARDWARE_TRIGGER]
        if blocked:
            return jsonify({
                "error": (
                    f"Camera(s) {blocked} are in hardware-trigger mode, which has no way to "
                    "fire an on-demand capture. Switch to Calibration/Regular mode on the Home "
                    "page first."
                ),
                "blocked_camera_ids": blocked,
            }), 409

        config.CAPTURE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=config.CAPTURE_TMP_DIR))

        results, errors, timed_out = capture_many(cameras, requested, tmp_dir)

        images: dict[int, str] = {}
        captured_ats: dict[int, str] = {}
        for cam_id, (path, metrics) in results.items():
            images[cam_id] = base64.b64encode(path.read_bytes()).decode("ascii")
            captured_ats[cam_id] = metrics.captured_at

        shutil.rmtree(tmp_dir, ignore_errors=True)

        if timed_out:
            logger.warning("debug_capture_timed_out", camera_ids=timed_out)
            return jsonify({
                "error": "Capture timed out waiting for camera(s)",
                "details": {str(k): "timed out after 15 s" for k in timed_out},
            }), 504

        status = 207 if errors else 200
        return jsonify({
            "images": {str(k): v for k, v in images.items()},
            "captured_at": {str(k): v for k, v in captured_ats.items()},
            "errors": {str(k): v for k, v in errors.items()},
        }), status

    # ── Upload ───────────────────────────────────────────────────────────

    @bp.route("/upload", methods=["POST"])
    def upload():
        """Forward one already-captured (EXIF-embedded) image to the debug
        destination, unchanged. Called once per image by the frontend when
        "Upload All" is clicked.
        """
        body = request.get_json(silent=True) or {}
        cam_id = body.get("camera_id")
        image_b64 = body.get("image_base64")
        captured_at = body.get("captured_at")
        if not isinstance(cam_id, int) or isinstance(cam_id, bool) or cam_id not in cameras:
            return jsonify({"error": f"camera_id must be one of {sorted(cameras.keys())}"}), 400
        if not image_b64:
            return jsonify({"error": "image_base64 is required"}), 400

        try:
            jpeg_bytes = base64.b64decode(image_b64)
        except Exception:
            return jsonify({"error": "image_base64 is not valid base64"}), 400

        if not _get_destination_url():
            return jsonify({"error": "No debug destination configured"}), 400

        serial = cameras[cam_id].serial_number or f"cam{cam_id}"
        ts_ms = int(time.time() * 1000)
        filename = f"{ts_ms}_{serial}_debug.jpg"
        if not isinstance(captured_at, str) or not captured_at:
            captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        ok, error = _post_debug_image(jpeg_bytes, filename, cam_id, captured_at)
        return jsonify({"uploaded": ok, "filename": filename, "error": error}), (200 if ok else 502)

    return bp
