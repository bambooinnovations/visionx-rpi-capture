import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


try:
    from PIL import Image, ImageFilter, ImageOps
except ImportError:
    print("Pillow is not installed. Please install it using 'pip install Pillow'")
    sys.exit(2)

from metrics import CaptureMetrics

RPI_64MP_SIZE = (9152, 6944)  # native resolution of the camera sensor
DEFAULT_IMAGE_SIZE = (4624, 3472)  # ~16MP
DEFAULT_CAPTURE_TMP_DIR = Path(os.environ.get("CAPTURE_TMP_DIR", "/tmp/visionx_captures"))


def run(cmd: list[str]) -> None:
    """Run command, raising on failure with nice output."""
    subprocess.run(cmd, check=True)


def capture_image(
    target_resolution: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    capture_resolution: tuple[int, int] = RPI_64MP_SIZE,
    output_folder: Path = DEFAULT_CAPTURE_TMP_DIR,
) -> tuple[Path, CaptureMetrics]:
    """Capture an image at full sensor resolution, then downscale to target_resolution.

    Args:
        target_resolution: Output image size in pixels (width, height).
        capture_resolution: Sensor capture size in pixels (width, height).
        output_folder: Directory where both the full-res and resized images are saved.

    Returns:
        Tuple of (path to resized output image, capture performance metrics).
    """
    rpicam = shutil.which("rpicam-jpeg")
    if not rpicam:
        raise RuntimeError("rpicam-jpeg not found in PATH. Please install it.")

    captured_at = datetime.now(timezone.utc).isoformat()
    current_time = int(time.time())
    full_image = output_folder / f"{current_time}_full.jpg"
    resized_image = output_folder / f"{current_time}_resized.jpg"

    command = [
        rpicam,
        "--width",
        str(capture_resolution[0]),
        "--height",
        str(capture_resolution[1]),
        "--nopreview",
        "-o",
        str(full_image),  # output path
    ]

    # capture the image at full resolution
    t0 = time.perf_counter()
    run(command)
    capture_duration_ms = (time.perf_counter() - t0) * 1000

    before_size = full_image.stat().st_size

    # downscale and optionally denoise/sharpen the image for better quality at the target resolution
    t1 = time.perf_counter()
    downscale_rpicam_jpeg(
        input_path=str(full_image),
        output_path=str(resized_image),
        target_size=target_resolution,
        denoise=True,
        sharpen=False,
    )
    downscale_duration_ms = (time.perf_counter() - t1) * 1000

    after_size = resized_image.stat().st_size

    metrics = CaptureMetrics(
        captured_at=captured_at,
        capture_duration_ms=capture_duration_ms,
        downscale_duration_ms=downscale_duration_ms,
        capture_width=capture_resolution[0],
        capture_height=capture_resolution[1],
        target_width=target_resolution[0],
        target_height=target_resolution[1],
        before_size_bytes=before_size,
        after_size_bytes=after_size,
    )

    return resized_image, metrics


def downscale_rpicam_jpeg(
    input_path: str,
    output_path: str,
    target_size: tuple[int, int],  # (width, height)
    denoise: bool = True,
    sharpen: bool = False,
):
    img = Image.open(input_path)

    # Respect EXIF orientation (rpicam-jpeg can write it)
    img = ImageOps.exif_transpose(img)

    # Work in RGB
    img = img.convert("RGB")

    # 1) Light denoise (JPEGs are already processed; keep this gentle)
    if denoise:
        # MedianFilter(3) is mild; good for speckle/salt-pepper-ish noise
        img = img.filter(ImageFilter.MedianFilter(size=3))

        # Slight blur can help mosquito noise; keep it tiny
        img = img.filter(ImageFilter.GaussianBlur(radius=0.3))

    # 2) High-quality downscale
    img = img.resize(target_size, resample=Image.Resampling.LANCZOS)

    # 3) Optional gentle sharpening (often NOT needed for rpicam-jpeg)
    if sharpen:
        img = img.filter(ImageFilter.UnsharpMask(radius=0.8, percent=60, threshold=3))

    # 4) Save: preserve chroma detail and avoid extra artifacts
    img.save(
        output_path,
        quality=95,
        subsampling=0,  # important for text/edges/defects
        optimize=True,
    )
