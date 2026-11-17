"""Helpers for the settings page's guided white balance, kept free of the SDK so they can be tested without a camera.

The wizard has the operator draw a box on a white patch (white paper, or the white square of a colour chart). The box is
{x, y, w, h} as fractions (0..1) of the displayed image, so the browser never needs the sensor resolution. The camera
itself does the white balance (CameraSetWbWindow + CameraSetOnceWB); these helpers only measure the box so the operator
can bring the patch's green level to about 200 first, as the vendor's procedure says, and check the result afterwards.
"""
from __future__ import annotations

import numpy as np

# The vendor's rule: the white patch's G channel should sit around 200 when white balance is run.
G_TARGET = 200
G_OK_LOW = 185
G_OK_HIGH = 215
# At or above this a channel is almost certainly clipped, and the gains cannot be trusted.
CLIP_LEVEL = 250
# Below this there is too little signal for the channel ratios to mean anything.
DARK_LEVEL = 40
# The box must cover enough pixels for a stable mean.
MIN_BOX_PIXELS = 400
# After white balance the patch should be neutral. A bigger R/G/B spread than this means the camera did not
# balance on the patch we drew (box misplaced, uneven light, or the SDK's window sits in other coordinates).
NEUTRAL_SPREAD_WARN = 8.0


def parse_box(box) -> tuple[float, float, float, float]:
    """Validate {x, y, w, h} (fractions of the image) and return the clamped corners (x0, y0, x1, y1)."""
    if not isinstance(box, dict):
        raise ValueError("box must be an object with x, y, w, h")
    try:
        x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("box needs numeric x, y, w, h") from None
    if not all(np.isfinite(v) for v in (x, y, w, h)):
        raise ValueError("box values must be finite")
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(1.0, x + w), min(1.0, y + h)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise ValueError("box is empty or outside the image")
    return x0, y0, x1, y1


def box_to_pixels(box, width: int, height: int) -> tuple[int, int, int, int]:
    """The box as (x, y, w, h) in pixels of a width x height image, always at least 1 px and inside the image."""
    x0, y0, x1, y1 = parse_box(box)
    px0 = min(int(round(x0 * width)), width - 1)
    py0 = min(int(round(y0 * height)), height - 1)
    px1 = max(px0 + 1, min(int(round(x1 * width)), width))
    py1 = max(py0 + 1, min(int(round(y1 * height)), height))
    return px0, py0, px1 - px0, py1 - py0


def box_stats(frame_bgr: np.ndarray, box) -> dict:
    """Mean R, G, B (0..255) inside `box`, plus the fraction of box pixels with any channel at 255.

    Raises ValueError for a mono frame or a box smaller than MIN_BOX_PIXELS.
    """
    if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
        raise ValueError("white balance needs a colour frame")
    h, w = frame_bgr.shape[:2]
    x, y, bw, bh = box_to_pixels(box, w, h)
    roi = frame_bgr[y:y + bh, x:x + bw]
    if bw * bh < MIN_BOX_PIXELS:
        raise ValueError("box is too small, draw a larger one")
    mean = roi.reshape(-1, 3).mean(axis=0)
    return {
        "r": float(mean[2]), "g": float(mean[1]), "b": float(mean[0]),
        "pixels": int(bw * bh), "clipped": float((roi.max(axis=2) >= 255).mean()),
    }


def classify(stats: dict) -> dict:
    """What the operator should do about the exposure: {state, message}.

    state is one of "dark", "low", "ok", "high", "clipped". Only "dark" and "clipped" make white balance unreliable.
    """
    g = stats["g"]
    peak = max(stats["r"], stats["g"], stats["b"])
    if stats.get("clipped", 0.0) > 0.02 or peak >= CLIP_LEVEL:
        return {"state": "clipped", "message": "The white area is too bright (a channel is clipping). Lower the exposure."}
    if g < DARK_LEVEL:
        return {"state": "dark", "message": "The white area is too dark. Raise the exposure, or check the box is on the white patch."}
    if g < G_OK_LOW:
        return {"state": "low", "message": f"A little dark. Raise the exposure until G is about {G_TARGET}."}
    if g > G_OK_HIGH:
        return {"state": "high", "message": f"A little bright. Lower the exposure until G is about {G_TARGET}."}
    return {"state": "ok", "message": "Exposure is good for white balance."}


def spread(stats: dict) -> float:
    """Largest difference between the R, G and B means, in levels."""
    vals = (stats["r"], stats["g"], stats["b"])
    return max(vals) - min(vals)
