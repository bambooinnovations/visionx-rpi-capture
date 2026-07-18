from __future__ import annotations

from pathlib import Path

import structlog

from camera.base import BaseCamera
from camera.mindvision import MindVisionCamera
from camera.picamera import PiCamera

import config

logger = structlog.get_logger()


def create_camera() -> BaseCamera:
    t = config.CAMERA_TYPE
    if t == "picamera2":
        return PiCamera()
    if t == "mindvision":
        return MindVisionCamera()
    raise ValueError(f"Unknown camera type: {t!r}. Expected 'auto', 'picamera2', or 'mindvision'.")


def build_camera_registry() -> dict[int, BaseCamera]:
    """Build the camera registry per config.CAMERA_TYPE.

    "mindvision" / "picamera2" — force that single type only (legacy
    single-type deployments; the other type is never probed).
    "auto" (default) — probe for MindVision devices and a Pi CSI camera
    independently and register whatever is actually present, mixing types
    in one registry. MindVision devices get the low ids (stable SDK
    enumeration order); a detected Pi camera gets the next id.

    Hardware trigger, mode switching, and stitching only ever apply to the
    MindVision subset of the registry — a Pi camera in a mixed registry
    just serves plain stream/capture, same as it would running alone.
    """
    t = config.CAMERA_TYPE

    if t == "mindvision":
        mv_cams = _detect_mindvision(required=True)
        return {cam.camera_index: cam for cam in mv_cams}

    if t == "picamera2":
        return {0: PiCamera()}

    if t != "auto":
        raise ValueError(f"Unknown camera type: {t!r}. Expected 'auto', 'picamera2', or 'mindvision'.")

    registry: dict[int, BaseCamera] = {}
    for cam in _detect_mindvision(required=False):
        registry[cam.camera_index] = cam

    if _picamera_present():
        registry[len(registry)] = PiCamera()

    if not registry:
        # Nothing detected — create one anyway (matches legacy default) so
        # open() surfaces a clear per-camera error instead of the app
        # refusing to start on a dev machine with no hardware attached.
        logger.warning("no_cameras_detected", fallback="picamera2")
        registry[0] = PiCamera()

    return registry


def _detect_mindvision(*, required: bool) -> list[MindVisionCamera]:
    """Enumerate connected MindVision devices via the SDK.

    Seeds the SDK-init/device-list cache in camera.mindvision so that
    MindVisionCamera.open() doesn't call CameraSdkInit a second time or
    re-enumerate (which may return a different order or omit
    already-initialized cameras).
    """
    count = 0
    try:
        import mvsdk
        import camera.mindvision as _mv_mod

        mvsdk.CameraSdkInit(0)  # 0 = English
        mvsdk.CameraSetDataDirectory(str(Path(__file__).parent.parent / "MindVisionCamera"))
        _mv_mod._sdk_initialized = True
        dev_list = mvsdk.CameraEnumerateDevice()
        _mv_mod._dev_list_cache = dev_list
        count = len(dev_list)
    except Exception as e:
        logger.warning("mindvision_enumerate_failed", reason=str(e))

    if count == 0 and required:
        count = 1  # create one anyway so open() surfaces a clear error

    logger.info("mindvision_cameras_detected", count=count)
    return [MindVisionCamera(camera_index=i) for i in range(count)]


def _picamera_present() -> bool:
    """Probe for a connected Pi CSI camera without opening/reserving it."""
    try:
        from picamera2 import Picamera2

        return len(Picamera2.global_camera_info()) > 0
    except Exception as e:
        logger.info("picamera_probe_failed", reason=str(e))
        return False
