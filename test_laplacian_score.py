"""Self-check for camera.picamera._laplacian_score (the cv2 rewrite).

Run: python test_laplacian_score.py
No framework — plain asserts. Guards the one non-trivial property: a sharp
image must score higher than a blurred copy, and a missing file yields 0.0.
"""
import tempfile
from pathlib import Path

import cv2
import numpy as np

from camera.picamera import _laplacian_score


def _checkerboard(size: int = 512, cell: int = 16) -> np.ndarray:
    idx = (np.arange(size) // cell) % 2
    return np.where(idx[:, None] ^ idx[None, :], 255, 0).astype(np.uint8)


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    sharp_path = tmp / "sharp.jpg"
    blur_path = tmp / "blur.jpg"

    sharp = _checkerboard()
    blur = cv2.GaussianBlur(sharp, (21, 21), 0)
    cv2.imwrite(str(sharp_path), sharp)
    cv2.imwrite(str(blur_path), blur)

    sharp_score = _laplacian_score(str(sharp_path))
    blur_score = _laplacian_score(str(blur_path))

    assert isinstance(sharp_score, float), sharp_score
    assert sharp_score > blur_score > 0, (sharp_score, blur_score)
    assert _laplacian_score(str(tmp / "nope.jpg")) == 0.0

    print(f"ok  sharp={sharp_score}  blur={blur_score}")


if __name__ == "__main__":
    main()
