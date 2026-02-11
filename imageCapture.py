import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from metrics import CaptureMetrics

DEFAULT_IMAGE_SIZE = (4624, 3472)  # ~16MP, full-sensor 2x2 binned mode
DEFAULT_CAPTURE_TMP_DIR = Path(os.environ.get("CAPTURE_TMP_DIR", "/tmp/visionx_captures"))


def capture_image(
    resolution: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    output_folder: Path = DEFAULT_CAPTURE_TMP_DIR,
) -> tuple[Path, CaptureMetrics]:
    """Capture an image using rpicam-still at the requested resolution.

    The ISP hardware scaler handles binning/scaling directly on-chip.
    For the ArduCam 64MP, (4624, 3472) uses the full sensor via 2x2 binning.
    """
    rpicam = shutil.which("rpicam-still")
    if not rpicam:
        raise RuntimeError("rpicam-still not found in PATH. Please install it.")

    captured_at = datetime.now(timezone.utc).isoformat()
    output_image = output_folder / f"{int(time.time())}.jpg"

    command = [
        rpicam,
        "--zsl",
        "--width", str(resolution[0]),
        "--height", str(resolution[1]),
        "--nopreview",
        "-o", str(output_image),
    ]

    t0 = time.perf_counter()
    subprocess.run(command, check=True)
    capture_duration_ms = (time.perf_counter() - t0) * 1000

    metrics = CaptureMetrics(
        captured_at=captured_at,
        capture_duration_ms=capture_duration_ms,
        width=resolution[0],
        height=resolution[1],
        file_size_bytes=output_image.stat().st_size,
    )

    return output_image, metrics
