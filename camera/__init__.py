from camera.base import BaseCamera
from camera.mindvision import MindVisionCamera
from camera.picamera import PiCamera

import config


def create_camera() -> BaseCamera:
    t = config.CAMERA_TYPE
    if t == "picamera2":
        return PiCamera()
    if t == "mindvision":
        return MindVisionCamera(camera_index=config.MV_CAMERA_INDEX)
    raise ValueError(f"Unknown camera type: {t!r}. Expected 'picamera2' or 'mindvision'.")
