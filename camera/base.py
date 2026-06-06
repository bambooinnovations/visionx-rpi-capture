from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import config
from metrics import CaptureMetrics


class BaseCamera(ABC):

    @abstractmethod
    def open(self) -> None:
        """Initialise hardware and start the camera."""

    @abstractmethod
    def close(self) -> None:
        """Stop streaming and release hardware resources."""

    def __enter__(self) -> "BaseCamera":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @abstractmethod
    def capture_image(
        self,
        resolution: tuple[int, int] | None = None,
        output_folder: Path = config.CAPTURE_TMP_DIR,
    ) -> tuple[Path, CaptureMetrics]:
        """Capture a high-quality still and return (path, metrics)."""

    @abstractmethod
    def stream_frames(self):
        """Yield raw JPEG bytes for each preview frame."""

    @abstractmethod
    def camera_info(self) -> dict:
        """Return a dict describing the camera and its current configuration."""
