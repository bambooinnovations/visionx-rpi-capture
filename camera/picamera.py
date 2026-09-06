from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import structlog

try:
    from picamera2 import Picamera2

    _PICAMERA2_AVAILABLE = True
except ImportError:
    Picamera2 = None  # type: ignore[assignment,misc]
    _PICAMERA2_AVAILABLE = False

import config
from camera.base import BaseCamera
from metrics import CaptureMetrics

logger = structlog.get_logger()


class PiCamera(BaseCamera):
    def __init__(self) -> None:
        self._cam: "Picamera2 | None" = None
        # Held by stream_frames() per-frame grab and by capture_image() for the
        # full AF + mode-switch + capture cycle, so the two never overlap.
        self._lock = threading.Lock()
        # Cached so capture_image() can restore preview mode without re-creating the dict.
        self._preview_config: dict | None = None
        self._capture_size: tuple[int, int] | None = None
        self._stream_size: tuple[int, int] | None = None
        # Embedded into every camera config so the lens position is applied from
        # the first frame after each start() — no post-start set_controls race.
        self._focus_controls: dict = {}

    def open(self) -> None:
        if not _PICAMERA2_AVAILABLE:
            raise RuntimeError(
                "picamera2 is not installed. On Raspberry Pi run: uv sync --extra rpi"
            )
        if self._cam is not None:
            return

        try:
            cam = Picamera2()
        except (IndexError, RuntimeError) as exc:
            raise RuntimeError(f"No camera detected: {exc}") from exc

        model = cam.camera_properties.get("Model", "")
        profile = config.get_camera_profile(model)

        if "stream_size" in profile:
            w, h = profile["stream_size"]
            stream_size = (int(w), int(h))
            logger.info("stream_size_source", source="camera_profile", model=model, size=stream_size)
        else:
            stream_size = (1280, 960)
            logger.info("stream_size_source", source="default", model=model, size=stream_size)

        self._stream_size = stream_size
        capture_size, raw_preview_size = self._select_sensor_modes(cam, stream_size)
        self._capture_size = capture_size

        logger.info(
            "sensor_modes_selected",
            capture_size=capture_size,
            raw_preview_size=raw_preview_size,
        )

        if "AfMode" in cam.camera_controls:
            if config.LENS_POSITION is not None:
                self._focus_controls = {"AfMode": 0, "LensPosition": config.LENS_POSITION}
                logger.info("focus_locked", lens_position=config.LENS_POSITION)
            else:
                self._focus_controls = {"AfMode": 2}

        # The raw stream pins libcamera to the full-FOV sensor mode so the
        # preview covers the same field of view as the still capture.
        self._preview_config = cam.create_preview_configuration(
            main={"size": stream_size},
            raw={"size": raw_preview_size},
            controls=self._focus_controls,
        )
        cam.configure(self._preview_config)
        cam.options["quality"] = 95
        cam.start()

        if config.LOCK_EXPOSURE:
            time.sleep(2)  # let AE/AWB converge under rig lighting before snapshotting
            meta = cam.capture_metadata()
            cam.set_controls({
                "AeEnable":     False,
                "AwbEnable":    False,
                "ExposureTime": meta["ExposureTime"],
                "AnalogueGain": meta["AnalogueGain"],
                "ColourGains":  meta["ColourGains"],
            })
            logger.info(
                "exposure_locked",
                exposure_us=meta["ExposureTime"],
                gain=round(meta["AnalogueGain"], 2),
            )

        self._cam = cam
        logger.info("camera_initialized", stream_size=stream_size, capture_size=capture_size)

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                logger.exception("picamera_close_failed")
            self._cam = None

    @staticmethod
    def _full_sensor_modes(modes: list[dict]) -> list[dict]:
        full_sensor = [
            m for m in modes
            if m.get("crop_limits", (1,))[0] == 0
            and m.get("crop_limits", (0, 1))[1] == 0
        ]
        return full_sensor or modes

    @staticmethod
    def _fastest_covering_mode(
        full_sensor: list[dict],
        target_size: tuple[int, int],
    ) -> tuple[int, int]:
        """Smallest-readout full-sensor mode whose size covers `target_size`,
        so the ISP scales/crops down from real sensor data instead of upsampling."""
        covering = [
            m for m in full_sensor
            if m["size"][0] >= target_size[0] and m["size"][1] >= target_size[1]
        ]
        if not covering:
            covering = full_sensor
        return max(covering, key=lambda m: m.get("fps", 0))["size"]

    def _select_sensor_modes(
        self,
        cam: "Picamera2",
        stream_size: tuple[int, int],
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        modes = cam.sensor_modes
        model = cam.camera_properties.get("Model", "")
        full_sensor = self._full_sensor_modes(modes)

        profile = config.get_camera_profile(model)

        if "capture_size" in profile:
            w, h = profile["capture_size"]
            capture_size = (int(w), int(h))
            logger.info("capture_size_source", source="camera_profile", model=model, size=capture_size)
        else:
            largest = max(full_sensor, key=lambda m: m["size"][0] * m["size"][1])
            capture_size = largest["size"]
            logger.info("capture_size_source", source="auto_detected", model=model, size=capture_size)

        # raw_preview_size: fastest full-sensor mode that still covers stream_size,
        # so libcamera doesn't crop the field of view just to feed the preview stream.
        raw_preview_size = self._fastest_covering_mode(full_sensor, stream_size)

        return capture_size, raw_preview_size

    def _configure_main_size(self, size: tuple[int, int]) -> None:
        """Reconfigure the live main stream to `size`. Also re-picks the raw
        sensor mode to cover it, so the ISP does real hardware scaling/cropping
        instead of digitally upsampling from a too-small raw capture."""
        full_sensor = self._full_sensor_modes(self._cam.sensor_modes)
        raw_size = self._fastest_covering_mode(full_sensor, size)
        cfg = self._cam.create_preview_configuration(
            main={"size": size},
            raw={"size": raw_size},
            controls=self._focus_controls,
        )
        self._cam.stop()
        self._cam.configure(cfg)
        self._cam.start()

    def stream_frames(
        self,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
    ):
        frame_interval = 1.0 / (fps if fps is not None else config.STREAM_FPS)

        # Resolved lazily, once the camera is open and self._stream_size is known.
        requested_size: tuple[int, int] | None = None
        configured_size: tuple[int, int] | None = None

        try:
            while True:
                if self._cam is None:
                    try:
                        self.open()
                        configured_size = None  # (re)opened at the default size
                    except RuntimeError:
                        logger.warning("stream_waiting_for_camera")
                        time.sleep(2)
                        continue

                if requested_size is None and (width is not None or height is not None):
                    native_width, native_height = self._stream_size
                    requested_size = (width or native_width, height or native_height)

                if requested_size is not None and configured_size != requested_size:
                    with self._lock:
                        self._configure_main_size(requested_size)
                    configured_size = requested_size

                start = time.monotonic()
                frame_data = None

                with self._lock:
                    try:
                        # capture_array("main") returns XBGR8888. The fourcc name
                        # describes the little-endian 32-bit word, so the bytes land
                        # in memory as R, G, B, X — i.e. RGB once the padding is
                        # dropped, NOT BGR. cv2 expects BGR, so convert instead of
                        # slicing; COLOR_RGBA2BGR drops the padding channel and swaps
                        # R/B in one vectorised pass (a [:, :, 2::-1] view would be
                        # non-contiguous and force imencode to copy).
                        # The ISP already scaled/cropped to the configured main size,
                        # so no software resize is needed here.
                        frame = cv2.cvtColor(
                            self._cam.capture_array("main"), cv2.COLOR_RGBA2BGR
                        )
                        ok, encoded = cv2.imencode(
                            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.STREAM_QUALITY]
                        )
                        if ok:
                            frame_data = encoded.tobytes()
                    except Exception:
                        logger.exception("stream_frame_skipped")

                if frame_data:
                    yield frame_data

                elapsed = time.monotonic() - start
                remaining = frame_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            # Restore the default stream size for whoever uses the camera next
            # (another stream request, or capture_image's post-still restore).
            if (
                configured_size is not None
                and configured_size != self._stream_size
                and self._cam is not None
            ):
                with self._lock:
                    self._configure_main_size(self._stream_size)

    def capture_image(
        self,
        resolution: tuple[int, int] | None = None,
        output_folder: Path = config.CAPTURE_TMP_DIR,
    ) -> tuple[Path, CaptureMetrics]:
        if self._cam is None:
            self.open()

        cam = self._cam
        if resolution is None:
            resolution = self._capture_size or (4624, 3472)

        captured_at = datetime.now(timezone.utc).isoformat()
        output_image = output_folder / f"{int(time.time())}.jpg"

        still_controls: dict = {
            "Sharpness": config.CAMERA_SHARPNESS,
            "NoiseReductionMode": 2,
            **self._focus_controls,
        }
        still_config = cam.create_still_configuration(
            main={"size": resolution},
            controls=still_controls,
        )

        t_wait0 = time.perf_counter()
        with self._lock:
            lock_wait_ms = (time.perf_counter() - t_wait0) * 1000

            t0 = time.perf_counter()
            if "AfMode" in cam.camera_controls and config.LENS_POSITION is None:
                success = cam.autofocus_cycle()
                if not success:
                    logger.warning("autofocus_failed", path=str(output_image))
            autofocus_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            cam.stop()
            cam.configure(still_config)
            cam.start()
            switch_to_still_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            cam.capture_file(str(output_image))
            capture_duration_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            cam.stop()
            cam.configure(self._preview_config)
            cam.start()
            switch_to_preview_ms = (time.perf_counter() - t0) * 1000

        stage_timings = {
            "lock_wait_ms": round(lock_wait_ms, 1),
            "autofocus_ms": round(autofocus_ms, 1),
            "switch_to_still_ms": round(switch_to_still_ms, 1),
            "capture_file_ms": round(capture_duration_ms, 1),
            "switch_to_preview_ms": round(switch_to_preview_ms, 1),
        }
        logger.info(
            "capture_stage_timings",
            path=str(output_image),
            total_ms=round(sum(stage_timings.values()), 1),
            **stage_timings,
        )

        sharpness = _laplacian_score(str(output_image))
        logger.info("capture_sharpness", score=sharpness, path=str(output_image))

        metrics = CaptureMetrics(
            captured_at=captured_at,
            capture_duration_ms=capture_duration_ms,
            width=resolution[0],
            height=resolution[1],
            file_size_bytes=output_image.stat().st_size,
            stage_timings=stage_timings,
        )
        return output_image, metrics

    def camera_info(self) -> dict:
        if self._cam is None:
            return {"type": "picamera2", "status": "closed"}
        return {
            "type": "picamera2",
            "model": self._cam.camera_properties.get("Model", "unknown"),
            "stream_size": self._stream_size,
            "capture_size": self._capture_size,
        }


def _laplacian_score(path: str) -> float:
    import numpy as np
    from PIL import Image
    arr = np.array(Image.open(path).convert("L"), dtype=np.float64)
    lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] +
           arr[1:-1, :-2] + arr[1:-1, 2:] - 4 * arr[1:-1, 1:-1])
    return round(float(lap.var()), 2)
