"""Flask blueprint for MindVision-specific camera endpoints.

Registered only when the active camera is a MindVisionCamera.
All routes are prefixed with /api/cameras.
"""
from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import zipfile
from collections import deque
from pathlib import Path

import config
import structlog
from camera.mindvision import CameraMode, MindVisionCamera
from flask import Blueprint, Response, after_this_request, jsonify, request, send_file

logger = structlog.get_logger()

# Per-camera rolling sharpness history for trend detection (camera_id -> deque).
_calibration_history: dict[int, deque] = {}

# Per-camera session peak score — never decreases within a session.
# Gives the bar a fixed ceiling so the user sees a real drop when they overshoot.
_peak_scores: dict[int, float] = {}

# Count of active calibration stream generators per camera. Used to avoid
# reverting the trigger mode while another calibration stream is still running.
_calibration_stream_count: dict[int, int] = {}

# ChArUco board for calibration stream detection overlay (lazy-initialised).
_charuco_board = None
_charuco_dict = None


def _get_charuco_board():
    global _charuco_board, _charuco_dict
    if _charuco_board is None:
        import cv2
        _charuco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
        _charuco_board = cv2.aruco.CharucoBoard((20, 14), 10.0, 8.0, _charuco_dict)
    return _charuco_board, _charuco_dict


def _frame_to_pil(frame: "np.ndarray") -> "PilImage.Image":
    """Convert a raw SDK frame (BGR/mono) to a PIL RGB image."""
    from PIL import Image as PilImage
    if frame.ndim == 3 and frame.shape[2] == 3:
        return PilImage.fromarray(frame[:, :, ::-1])  # BGR → RGB
    if frame.ndim == 3 and frame.shape[2] == 1:
        return PilImage.fromarray(frame[:, :, 0], mode="L").convert("RGB")
    return PilImage.fromarray(frame, mode="L").convert("RGB")


def _encode_raw_frame(frame: "np.ndarray", max_width: int, quality: int = 85) -> bytes:
    """Resize frame and return a plain JPEG with no overlay."""
    from PIL import Image as PilImage
    pil_img = _frame_to_pil(frame)

    orig_w, orig_h = pil_img.size
    if orig_w > max_width:
        new_h = int(orig_h * max_width / orig_w)
        pil_img = pil_img.resize((max_width, new_h), PilImage.BILINEAR)

    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _render_calibration_overlay(
    frame: "np.ndarray",
    history: "deque[float]",
    peak_threshold: int,
    max_width: int,
    camera_id: int = 0,
    detect_charuco: bool = False,
    clip_highlight: bool = True,
) -> bytes:
    """Return a JPEG with focus peaking + info overlay.

    All text is stacked in the top-left corner.  The sharpness bar stays
    at the bottom.  Overlay lines (top to bottom):
      cam{id}
      Score: {n}  {trend arrow} {suggestion}
      Clipped: {n}%  {arrow} {exposure suggestion}
      ChArUco: {n} corners — {ready / too few / not detected}
    """
    import cv2 as _cv2
    import numpy as np
    from PIL import Image as PilImage, ImageDraw, ImageFont

    # frame is HxWx3 BGR (color) or HxWx1 (mono) from the SDK.
    pil_img = _frame_to_pil(frame)

    orig_w, orig_h = pil_img.size
    if orig_w > max_width:
        new_h = int(orig_h * max_width / orig_w)
        pil_img = pil_img.resize((max_width, new_h), PilImage.BILINEAR)

    w, h = pil_img.size
    rgb = np.array(pil_img, dtype=np.uint8)

    # Grayscale for gradient computation and ChArUco detection.
    gray = (
        0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    ).astype(np.float32)
    gray_u8 = gray.astype(np.uint8)

    # Laplacian variance on the center third — standard single-frame sharpness metric.
    roi_y1, roi_y2 = h // 3, 2 * h // 3
    roi_x1, roi_x2 = w // 3, 2 * w // 3
    roi = gray[roi_y1:roi_y2, roi_x1:roi_x2]
    lap = (
        roi[:-2, 1:-1] + roi[2:, 1:-1] + roi[1:-1, :-2] + roi[1:-1, 2:]
        - 4.0 * roi[1:-1, 1:-1]
    )
    score = float(np.var(lap))
    history.append(score)

    # Session peak — never decreases so the bar has a fixed ceiling.
    session_peak = max(_peak_scores.get(camera_id, 0.0), score)
    _peak_scores[camera_id] = session_peak
    pct_of_peak = score / max(session_peak, 1.0)

    # Trend: compare first half vs. second half of rolling window.
    rel = 0.0
    if len(history) >= 6:
        arr = list(history)
        mid = len(arr) // 2
        first_mean = float(np.mean(arr[:mid]))
        last_mean = float(np.mean(arr[mid:]))
        rel = (last_mean - first_mean) / max(abs(first_mean), 1.0)
        if rel > 0.05:
            trend_char, trend_color = "↑", (80, 220, 80)
        elif rel < -0.05:
            trend_char, trend_color = "↓", (220, 80, 80)
        else:
            trend_char, trend_color = "●", (220, 220, 80)
        have_trend = True
    else:
        trend_char, trend_color = "~", (180, 180, 180)
        have_trend = False

    # Large instruction for the operator — one clear action at a time.
    if not have_trend or session_peak < 5.0:
        instr_text = "ADJUSTING..."
        instr_bg = (50, 50, 50)
        instr_fg = (180, 180, 180)
    elif pct_of_peak >= 0.93:
        instr_text = "BEST FOCUS"
        instr_bg = (20, 150, 40)
        instr_fg = (255, 255, 255)
    elif rel > 0.05:
        instr_text = "KEEP TURNING"
        instr_bg = (140, 100, 0)
        instr_fg = (255, 255, 255)
    else:
        instr_text = "TURN BACK"
        instr_bg = (180, 30, 30)
        instr_fg = (255, 255, 255)

    # Exposure analysis: fraction of pixels with any channel clipped (≥ 250).
    clip_mask = np.any(rgb >= 250, axis=2)
    clipped_pct = 100.0 * float(clip_mask.sum()) / (h * w)
    mean_brightness = float(gray.mean())

    if clipped_pct > 5.0:
        exp_char, exp_color, exp_suggestion = "▲", (220, 60, 60), "Close aperture"
    elif clipped_pct > 0.5:
        exp_char, exp_color, exp_suggestion = "▲", (220, 160, 40), "Slightly overexposed"
    elif mean_brightness < 30:
        exp_char, exp_color, exp_suggestion = "▼", (80, 160, 220), "Open aperture"
    elif mean_brightness < 60:
        exp_char, exp_color, exp_suggestion = "▼", (140, 200, 240), "Slightly underexposed"
    else:
        exp_char, exp_color, exp_suggestion = "●", (80, 220, 80), "Exposure OK"

    # ChArUco detection — only when explicitly requested.
    if detect_charuco:
        try:
            board, aruco_dict = _get_charuco_board()
            detector = _cv2.aruco.ArucoDetector(aruco_dict)
            marker_corners, marker_ids, _ = detector.detectMarkers(gray_u8)
            n_charuco = 0
            if marker_ids is not None and len(marker_ids) >= 4:
                _, charuco_corners, _ = _cv2.aruco.interpolateCornersCharuco(
                    marker_corners, marker_ids, gray_u8, board
                )
                if charuco_corners is not None:
                    n_charuco = len(charuco_corners)
        except Exception:
            n_charuco = -1

        if n_charuco < 0:
            charuco_text, charuco_color = "ChArUco: unavailable", (180, 180, 180)
        elif n_charuco >= 8:
            charuco_text, charuco_color = f"ChArUco: {n_charuco} corners — ready", (80, 220, 80)
        elif n_charuco > 0:
            charuco_text, charuco_color = f"ChArUco: {n_charuco} corners — too few", (220, 160, 40)
        else:
            charuco_text, charuco_color = "ChArUco: not detected", (220, 80, 80)
    else:
        charuco_text, charuco_color = None, None

    # Focus peaking: highlight pixels where |gradient| > threshold in magenta.
    gi = gray.astype(np.int32)
    gx = np.abs(gi[1:-1, 2:] - gi[1:-1, :-2])
    gy = np.abs(gi[2:, 1:-1] - gi[:-2, 1:-1])
    gradient = np.zeros((h, w), dtype=np.int32)
    gradient[1:-1, 1:-1] = gx + gy
    peak_mask = gradient > peak_threshold

    rgb_out = rgb.copy()
    rgb_out[peak_mask] = [255, 0, 255]
    # Clipped pixels painted red on top — more critical than focus-peak markers.
    if clip_highlight:
        rgb_out[clip_mask] = [255, 40, 40]

    pil_out = PilImage.fromarray(rgb_out).convert("RGBA")
    draw = ImageDraw.Draw(pil_out)

    font_size = max(16, w // 48)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size
        )
        font_sm = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max(12, font_size - 4)
        )
    except Exception:
        font = ImageFont.load_default()
        font_sm = font

    # ROI box.
    draw.rectangle([roi_x1, roi_y1, roi_x2, roi_y2], outline=(255, 255, 0), width=2)

    # ── Top-left info stack ───────────────────────────────────────────────────
    pad = max(8, w // 80)
    line_gap = font_size + 6
    y = pad

    lines = [
        (f"cam{camera_id}", (255, 255, 255)),
        (f"Score: {score:.0f}  {trend_char}  ({pct_of_peak * 100:.0f}% of best)", trend_color),
        (f"Clipped: {clipped_pct:.1f}%  {exp_char} {exp_suggestion}", exp_color),
        *(([(charuco_text, charuco_color)]) if charuco_text else []),
    ]
    for text, color in lines:
        draw.text((pad, y), text, fill=color, font=font)
        y += line_gap

    # ── Large instruction banner (bottom-center) ──────────────────────────────
    banner_font_size = max(28, w // 22)
    try:
        banner_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", banner_font_size
        )
    except Exception:
        banner_font = font

    banner_pad_x = w // 4
    banner_h = banner_font_size + pad * 3
    banner_y1 = h - banner_h - pad
    banner_y2 = h - pad
    banner_x1 = banner_pad_x
    banner_x2 = w - banner_pad_x
    draw.rectangle([banner_x1, banner_y1, banner_x2, banner_y2], fill=instr_bg)
    bbox = draw.textbbox((0, 0), instr_text, font=banner_font)
    txt_w = bbox[2] - bbox[0]
    txt_h = bbox[3] - bbox[1]
    txt_x = (w - txt_w) // 2
    txt_y = banner_y1 + (banner_h - txt_h) // 2
    draw.text((txt_x, txt_y), instr_text, fill=instr_fg, font=banner_font)

    # ── Sharpness bar (above the instruction banner) ──────────────────────────
    # Scale is fixed to session_peak so a drop is immediately visible.
    bar_total = w // 3
    bar_x1 = pad
    bar_y2 = banner_y1 - pad
    bar_y1 = bar_y2 - max(14, font_size // 2)
    fill_frac = min(pct_of_peak, 1.0)
    fill_x2 = bar_x1 + int(fill_frac * bar_total)
    bar_fill = (
        (80, 200, 80) if fill_frac > 0.8 else
        (220, 200, 60) if fill_frac > 0.5 else
        (200, 80, 80)
    )
    draw.rectangle([bar_x1, bar_y1, bar_x1 + bar_total, bar_y2], outline=(200, 200, 200), width=1)
    if fill_x2 > bar_x1:
        draw.rectangle([bar_x1 + 1, bar_y1 + 1, fill_x2, bar_y2 - 1], fill=bar_fill)
    # Peak tick at 100%.
    peak_x = bar_x1 + bar_total
    draw.line([peak_x, bar_y1 - 4, peak_x, bar_y2 + 4], fill=(255, 255, 255), width=2)
    draw.text(
        (bar_x1, bar_y1 - font_size - 2),
        "Sharpness  (|  = session best)",
        fill=(200, 200, 200),
        font=font_sm,
    )

    buf = io.BytesIO()
    pil_out.convert("RGB").save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def _render_lens_stream_frame(
    frame: "np.ndarray",
    max_width: int,
    guide_pct: int,
    camera_id: int,
    cx_frac: float = 0.5,
    cy_frac: float = 0.5,
) -> bytes:
    """Return a JPEG with guide box + ChArUco overlay only (no focus peaking)."""
    import cv2 as _cv2
    import numpy as np
    from PIL import Image as PilImage, ImageDraw, ImageFont

    pil_img = _frame_to_pil(frame)

    orig_w, orig_h = pil_img.size
    if orig_w > max_width:
        new_h = int(orig_h * max_width / orig_w)
        pil_img = pil_img.resize((max_width, new_h), PilImage.BILINEAR)

    w, h = pil_img.size
    rgb = np.array(pil_img, dtype=np.uint8)
    gray_u8 = _cv2.cvtColor(rgb, _cv2.COLOR_RGB2GRAY)

    # guide_pct=0 means free mode — skip guide box entirely.
    show_guide = guide_pct > 0

    # Guide box matches the physical board aspect ratio: 20 cols × 14 rows = 200 mm × 140 mm.
    _BOARD_COLS, _BOARD_ROWS = 20, 14
    guide_pct = max(10, min(90, guide_pct)) if show_guide else 40
    box_w = int(w * guide_pct / 100)
    box_h = int(box_w * _BOARD_ROWS / _BOARD_COLS)
    if box_h > int(h * 0.9):
        box_h = int(h * 0.9)
        box_w = int(box_h * _BOARD_COLS / _BOARD_ROWS)
    # cx/cy are the desired box-centre as fractions of frame size (default: centred).
    box_cx = int(w * cx_frac)
    box_cy = int(h * cy_frac)
    box_x1 = max(0, min(w - box_w, box_cx - box_w // 2))
    box_y1 = max(0, min(h - box_h, box_cy - box_h // 2))
    box_x2 = box_x1 + box_w
    box_y2 = box_y1 + box_h
    guide_diag = (box_w ** 2 + box_h ** 2) ** 0.5

    # ChArUco detection.
    charuco_corners = None
    charuco_diag = None
    try:
        board, aruco_dict = _get_charuco_board()
        detector = _cv2.aruco.ArucoDetector(aruco_dict)
        marker_corners, marker_ids, _ = detector.detectMarkers(gray_u8)
        if marker_ids is not None and len(marker_ids) >= 4:
            _, corners, _ = _cv2.aruco.interpolateCornersCharuco(
                marker_corners, marker_ids, gray_u8, board
            )
            if corners is not None and len(corners) >= 6:
                charuco_corners = corners
                pts = corners.reshape(-1, 2)
                cx1, cy1 = pts[:, 0].min(), pts[:, 1].min()
                cx2, cy2 = pts[:, 0].max(), pts[:, 1].max()
                charuco_diag = ((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2) ** 0.5
    except Exception:
        pass

    # Draw charuco corner dots.
    if charuco_corners is not None:
        for pt in charuco_corners.reshape(-1, 2):
            _cv2.circle(rgb, (int(pt[0]), int(pt[1])), 4, (0, 220, 0), -1)

    pil_out = PilImage.fromarray(rgb).convert("RGBA")
    draw = ImageDraw.Draw(pil_out)

    if show_guide:
        def _dashed_line(d, x1, y1, x2, y2, dash=14, gap=8, color=(255, 255, 255), lw=2):
            total = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if total == 0:
                return
            ux, uy = (x2 - x1) / total, (y2 - y1) / total
            pos = 0.0
            on = True
            while pos < total:
                seg = dash if on else gap
                end = min(pos + seg, total)
                if on:
                    d.line(
                        [(x1 + ux * pos, y1 + uy * pos), (x1 + ux * end, y1 + uy * end)],
                        fill=color,
                        width=lw,
                    )
                pos = end
                on = not on

        for sx1, sy1, sx2, sy2 in [
            (box_x1, box_y1, box_x2, box_y1),
            (box_x2, box_y1, box_x2, box_y2),
            (box_x2, box_y2, box_x1, box_y2),
            (box_x1, box_y2, box_x1, box_y1),
        ]:
            _dashed_line(draw, sx1, sy1, sx2, sy2)

        bk = max(20, min(box_w, box_h) // 10)
        cyan = (0, 220, 255)
        for cx, cy, dx, dy in [
            (box_x1, box_y1, 1, 1),
            (box_x2, box_y1, -1, 1),
            (box_x2, box_y2, -1, -1),
            (box_x1, box_y2, 1, -1),
        ]:
            draw.line([(cx, cy), (cx + dx * bk, cy)], fill=cyan, width=3)
            draw.line([(cx, cy), (cx, cy + dy * bk)], fill=cyan, width=3)

    # Fonts.
    font_size = max(16, w // 50)
    banner_font_size = max(20, w // 36)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        banner_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", banner_font_size
        )
    except Exception:
        font = ImageFont.load_default()
        banner_font = font

    # Feedback message.
    if not show_guide:
        if charuco_corners is None:
            msg = "Place board anywhere in frame"
            msg_fg = (180, 180, 180)
            msg_bg = (30, 30, 30, 180)
        else:
            msg = "Board detected — ready to capture"
            msg_fg = (80, 255, 120)
            msg_bg = (0, 50, 20, 200)
    elif charuco_corners is None:
        msg = "No board detected"
        msg_fg = (220, 80, 80)
        msg_bg = (50, 10, 10, 200)
    else:
        ratio = charuco_diag / max(guide_diag, 1.0)
        if ratio < 0.65:
            msg = "Come closer — board is too small"
            msg_fg = (255, 180, 40)
            msg_bg = (60, 40, 0, 200)
        elif ratio > 1.45:
            msg = "Move back — board is too large"
            msg_fg = (100, 200, 255)
            msg_bg = (0, 20, 60, 200)
        else:
            pts = charuco_corners.reshape(-1, 2)
            inside_frac = float(
                ((pts[:, 0] >= box_x1) & (pts[:, 0] <= box_x2) &
                 (pts[:, 1] >= box_y1) & (pts[:, 1] <= box_y2)).mean()
            )
            if inside_frac >= 0.7:
                msg = "Hold still — ready to capture"
                msg_fg = (80, 255, 120)
                msg_bg = (0, 50, 20, 200)
            else:
                msg = "Center the board in the box"
                msg_fg = (255, 220, 60)
                msg_bg = (50, 40, 0, 200)

    pad = max(8, w // 80)
    bbox = draw.textbbox((0, 0), msg, font=banner_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bx1 = max(0, (w - tw) // 2 - pad * 2)
    by1 = h - th - pad * 3
    bx2 = min(w, (w + tw) // 2 + pad * 2)
    by2 = h - pad

    overlay = PilImage.new("RGBA", pil_out.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle([bx1, by1, bx2, by2], fill=msg_bg)
    pil_out = PilImage.alpha_composite(pil_out, overlay)
    draw2 = ImageDraw.Draw(pil_out)
    draw2.text(((w - tw) // 2, by1 + pad), msg, fill=msg_fg, font=banner_font)
    draw2.text((pad, pad), f"cam{camera_id}", fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    pil_out.convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _read_mv_settings(h: int, cap) -> dict:
    """Read all tunable SDK parameters for a camera handle."""
    import mvsdk
    s = {}

    try: s["ae_enabled"] = bool(mvsdk.CameraGetAeState(h))
    except Exception: s["ae_enabled"] = True

    try: s["exposure_us"] = mvsdk.CameraGetExposureTime(h)
    except Exception: s["exposure_us"] = 30000.0

    try:
        exp_min, exp_max, _ = mvsdk.CameraGetExposureTimeRange(h)
        s["exposure_min_us"] = exp_min
        s["exposure_max_us"] = exp_max
    except Exception:
        s["exposure_min_us"] = 26.0
        s["exposure_max_us"] = 1_000_000.0

    try: s["ae_target"] = mvsdk.CameraGetAeTarget(h)
    except Exception: s["ae_target"] = 100

    try:
        s["analog_gain"] = mvsdk.CameraGetAnalogGain(h)
        s["analog_gain_min"] = cap.sExposeDesc.uiAnalogGainMin if cap else 16
        s["analog_gain_max"] = cap.sExposeDesc.uiAnalogGainMax if cap else 128
    except Exception:
        s.update(analog_gain=16, analog_gain_min=16, analog_gain_max=128)

    try:
        r, g, b = mvsdk.CameraGetGain(h)
        s.update(r_gain=r, g_gain=g, b_gain=b)
        if cap:
            s.update(
                r_gain_min=cap.sRgbGainRange.iRGainMin, r_gain_max=cap.sRgbGainRange.iRGainMax,
                g_gain_min=cap.sRgbGainRange.iGGainMin, g_gain_max=cap.sRgbGainRange.iGGainMax,
                b_gain_min=cap.sRgbGainRange.iBGainMin, b_gain_max=cap.sRgbGainRange.iBGainMax,
            )
        else:
            s.update(r_gain_min=0, r_gain_max=400, g_gain_min=0, g_gain_max=400,
                     b_gain_min=0, b_gain_max=400)
    except Exception:
        s.update(r_gain=100, g_gain=100, b_gain=100,
                 r_gain_min=0, r_gain_max=400, g_gain_min=0, g_gain_max=400,
                 b_gain_min=0, b_gain_max=400)

    try:
        s["sharpness"] = mvsdk.CameraGetSharpness(h)
        s["sharpness_min"] = cap.sSharpnessRange.iMin if cap else 0
        s["sharpness_max"] = cap.sSharpnessRange.iMax if cap else 100
    except Exception:
        s.update(sharpness=0, sharpness_min=0, sharpness_max=100)

    try:
        s["gamma"] = mvsdk.CameraGetGamma(h)
        s["gamma_min"] = cap.sGammaRange.iMin if cap else 0
        s["gamma_max"] = cap.sGammaRange.iMax if cap else 250
    except Exception:
        s.update(gamma=100, gamma_min=0, gamma_max=250)

    try:
        s["rotation"] = mvsdk.CameraGetRotate(h)
        s["h_mirror"] = bool(mvsdk.CameraGetMirror(h, 0))
        s["v_mirror"] = bool(mvsdk.CameraGetMirror(h, 1))
    except Exception:
        s.update(rotation=0, h_mirror=False, v_mirror=False)

    return s


def _apply_mv_settings(h: int, body: dict) -> tuple[list[str], dict[str, str]]:
    """Apply body fields to camera hardware without saving. Returns (applied, errors)."""
    import mvsdk
    applied: list[str] = []
    errors: dict[str, str] = {}

    if "ae_enabled" in body:
        try:
            mvsdk.CameraSetAeState(h, 1 if body["ae_enabled"] else 0)
            applied.append("ae_enabled")
        except Exception as exc:
            errors["ae_enabled"] = str(exc)

    # Skip manual exposure when AE is being enabled — setting exposure time
    # while AE is on can cause some SDK builds to silently disable AE.
    ae_on = body.get("ae_enabled", None)
    if "exposure_us" in body and ae_on is not True:
        try:
            mvsdk.CameraSetExposureTime(h, float(body["exposure_us"]))
            applied.append("exposure_us")
        except Exception as exc:
            errors["exposure_us"] = str(exc)

    if "ae_target" in body:
        try:
            mvsdk.CameraSetAeTarget(h, int(body["ae_target"]))
            applied.append("ae_target")
        except Exception as exc:
            errors["ae_target"] = str(exc)

    if "analog_gain" in body:
        try:
            mvsdk.CameraSetAnalogGain(h, int(body["analog_gain"]))
            applied.append("analog_gain")
        except Exception as exc:
            errors["analog_gain"] = str(exc)

    rgb_keys = ("r_gain", "g_gain", "b_gain")
    if any(k in body for k in rgb_keys):
        try:
            r, g, b = mvsdk.CameraGetGain(h)
            mvsdk.CameraSetGain(
                h,
                int(body.get("r_gain", r)),
                int(body.get("g_gain", g)),
                int(body.get("b_gain", b)),
            )
            applied.extend(k for k in rgb_keys if k in body)
        except Exception as exc:
            errors["rgb_gain"] = str(exc)

    if "sharpness" in body:
        try:
            mvsdk.CameraSetSharpness(h, int(body["sharpness"]))
            applied.append("sharpness")
        except Exception as exc:
            errors["sharpness"] = str(exc)

    if "gamma" in body:
        try:
            mvsdk.CameraSetGamma(h, int(body["gamma"]))
            applied.append("gamma")
        except Exception as exc:
            errors["gamma"] = str(exc)

    if "rotation" in body:
        try:
            rot = int(body["rotation"])
            if rot not in (0, 1, 2, 3):
                raise ValueError("must be 0–3")
            mvsdk.CameraSetRotate(h, rot)
            applied.append("rotation")
        except Exception as exc:
            errors["rotation"] = str(exc)

    if "h_mirror" in body:
        try:
            mvsdk.CameraSetMirror(h, 0, int(bool(body["h_mirror"])))
            applied.append("h_mirror")
        except Exception as exc:
            errors["h_mirror"] = str(exc)

    if "v_mirror" in body:
        try:
            mvsdk.CameraSetMirror(h, 1, int(bool(body["v_mirror"])))
            applied.append("v_mirror")
        except Exception as exc:
            errors["v_mirror"] = str(exc)

    return applied, errors


def _read_full_config(h: int, cam: "MindVisionCamera") -> dict:
    """Read every available SDK parameter for a camera, grouped by category.

    Returns {"camera_id": int, "groups": [{"id": str, "name": str, "params": [...]}]}.
    Each param is {"key": str, "label": str, "value": any, "unit": str|None}.
    All calls are wrapped; unsupported params surface as null values.
    """
    import mvsdk

    def safe(fn, *args, default=None):
        try:
            return fn(*args)
        except Exception:
            return default

    def p(key, label, value, unit=None):
        return {"key": key, "label": label, "value": value, "unit": unit}

    info = cam.camera_info()
    cap  = cam._cap

    # ── Device ────────────────────────────────────────────────────────────
    device_params = [
        p("serial_number",     "Serial Number",       cam.serial_number),
        p("model",             "Model",               info.get("model")),
        p("product_name",      "Product Name",        info.get("product_name")),
        p("port_type",         "Port Type",           info.get("port_type")),
        p("firmware_version",  "Firmware Version",    safe(mvsdk.CameraGetFirmwareVersion, h)),
        p("interface_version", "Interface Version",   safe(mvsdk.CameraGetInerfaceVersion, h)),
        p("friendly_name",     "Friendly Name",       safe(mvsdk.CameraGetFriendlyName, h)),
        p("parameter_group",   "Parameter Group",     safe(mvsdk.CameraGetCurrentParameterGroup, h)),
        p("auto_connect",      "Auto Reconnect",      bool(safe(mvsdk.CameraGetAutoConnect, h, default=0))),
        p("reconnect_count",   "Reconnect Count",     safe(mvsdk.CameraGetReConnectCounts, h)),
    ]

    # ── Image & Resolution ────────────────────────────────────────────────
    res  = safe(mvsdk.CameraGetImageResolution, h)
    snap = safe(mvsdk.CameraGetResolutionForSnap, h)
    image_params = [
        p("width",          "Width",              res.iWidth            if res  else None, "px"),
        p("height",         "Height",             res.iHeight           if res  else None, "px"),
        # From camera_profiles.<model> as loaded at process start — compare
        # against width/height above to check the running process actually
        # picked up the profile you expect (config.toml isn't hot-reloaded).
        p("configured_capture_size", "Configured Capture Size", list(cam._capture_size) if cam._capture_size else None),
        p("configured_stream_size",  "Configured Stream Size",  list(cam._stream_size) if cam._stream_size else None),
        p("width_fov",      "FOV Width",          res.iWidthFOV         if res  else None, "px"),
        p("height_fov",     "FOV Height",         res.iHeightFOV        if res  else None, "px"),
        p("h_offset_fov",   "FOV H Offset",       res.iHOffsetFOV       if res  else None, "px"),
        p("v_offset_fov",   "FOV V Offset",       res.iVOffsetFOV       if res  else None, "px"),
        p("bin_sum_mode",   "Bin Sum Mode",       res.uBinSumMode       if res  else None),
        p("skip_mode",      "Skip Mode",          res.uSkipMode         if res  else None),
        p("snap_width",     "Snap Width",         snap.iWidth           if snap else None, "px"),
        p("snap_height",    "Snap Height",        snap.iHeight          if snap else None, "px"),
        p("media_type",     "Media Type",         safe(mvsdk.CameraGetMediaType, h)),
        p("isp_out_format", "ISP Output Format",  safe(mvsdk.CameraGetIspOutFormat, h)),
        p("isp_processor",  "ISP Processor",      safe(mvsdk.CameraGetIspProcessor, h)),
        p("monochrome",     "Monochrome Mode",    bool(safe(mvsdk.CameraGetMonochrome, h, default=0))),
        p("inverse",        "Invert Image",       bool(safe(mvsdk.CameraGetInverse, h, default=0))),
        p("black_level",    "Black Level",        safe(mvsdk.CameraGetBlackLevel, h)),
        p("white_level",    "White Level",        safe(mvsdk.CameraGetWhiteLevel, h)),
    ]

    # ── Exposure & AE ─────────────────────────────────────────────────────
    exp_range    = safe(mvsdk.CameraGetExposureTimeRange, h) or (None, None, None)
    ae_exp_range = safe(mvsdk.CameraGetAeExposureRange, h)   or (None, None)
    ae_gain_rng  = safe(mvsdk.CameraGetAeAnalogGainRange, h) or (None, None)
    ae_win       = safe(mvsdk.CameraGetAeWindow, h)           or (None, None, None, None)
    isp_proc     = safe(mvsdk.CameraGetIspProcessor, h, default=0)
    exposure_params = [
        p("ae_enabled",          "Auto Exposure",            bool(safe(mvsdk.CameraGetAeState, h, default=0))),
        p("exposure_us",         "Exposure Time",            safe(mvsdk.CameraGetExposureTime, h), "µs"),
        p("exposure_min_us",     "Exposure Min",             exp_range[0],                          "µs"),
        p("exposure_max_us",     "Exposure Max",             exp_range[1],                          "µs"),
        p("exposure_step_us",    "Exposure Step",            exp_range[2],                          "µs"),
        p("exposure_line_us",    "Line Time",                safe(mvsdk.CameraGetExposureLineTime, h), "µs"),
        p("ae_target",           "AE Target Brightness",     safe(mvsdk.CameraGetAeTarget, h)),
        p("ae_threshold",        "AE Threshold",             safe(mvsdk.CameraGetAeThreshold, h)),
        p("ae_algorithm",        "AE Algorithm",             safe(mvsdk.CameraGetAeAlgorithm, h, isp_proc)),
        p("ae_exp_min_us",       "AE Exposure Min",          ae_exp_range[0],                       "µs"),
        p("ae_exp_max_us",       "AE Exposure Max",          ae_exp_range[1],                       "µs"),
        p("ae_gain_min",         "AE Gain Min",              ae_gain_rng[0]),
        p("ae_gain_max",         "AE Gain Max",              ae_gain_rng[1]),
        p("ae_window_h_off",     "AE Window H Offset",       ae_win[0],                             "px"),
        p("ae_window_v_off",     "AE Window V Offset",       ae_win[1],                             "px"),
        p("ae_window_width",     "AE Window Width",          ae_win[2],                             "px"),
        p("ae_window_height",    "AE Window Height",         ae_win[3],                             "px"),
        p("anti_flick",          "Anti-Flicker",             bool(safe(mvsdk.CameraGetAntiFlick, h, default=0))),
        p("light_frequency",     "Light Frequency",          safe(mvsdk.CameraGetLightFrequency, h)),
        p("frame_speed",         "Frame Speed Index",        safe(mvsdk.CameraGetFrameSpeed, h)),
    ]

    # ── Gain ──────────────────────────────────────────────────────────────
    rgb          = safe(mvsdk.CameraGetGain, h) or (None, None, None)
    gainx_range  = safe(mvsdk.CameraGetAnalogGainXRange, h) or (None, None, None)
    gain_params = [
        p("analog_gain",        "Analog Gain",         safe(mvsdk.CameraGetAnalogGain, h)),
        p("analog_gain_min",    "Analog Gain Min",     cap.sExposeDesc.uiAnalogGainMin  if cap else None),
        p("analog_gain_max",    "Analog Gain Max",     cap.sExposeDesc.uiAnalogGainMax  if cap else None),
        p("analog_gain_step",   "Analog Gain Step",    cap.sExposeDesc.fAnalogGainStep  if cap else None),
        p("analog_gain_x",      "Analog Gain X",       safe(mvsdk.CameraGetAnalogGainX, h)),
        p("analog_gain_x_min",  "Analog Gain X Min",   gainx_range[0]),
        p("analog_gain_x_max",  "Analog Gain X Max",   gainx_range[1]),
        p("analog_gain_x_step", "Analog Gain X Step",  gainx_range[2]),
        p("r_gain",             "R Gain",              rgb[0]),
        p("g_gain",             "G Gain",              rgb[1]),
        p("b_gain",             "B Gain",              rgb[2]),
        p("r_gain_min",         "R Gain Min",          cap.sRgbGainRange.iRGainMin if cap else None),
        p("r_gain_max",         "R Gain Max",          cap.sRgbGainRange.iRGainMax if cap else None),
        p("g_gain_min",         "G Gain Min",          cap.sRgbGainRange.iGGainMin if cap else None),
        p("g_gain_max",         "G Gain Max",          cap.sRgbGainRange.iGGainMax if cap else None),
        p("b_gain_min",         "B Gain Min",          cap.sRgbGainRange.iBGainMin if cap else None),
        p("b_gain_max",         "B Gain Max",          cap.sRgbGainRange.iBGainMax if cap else None),
    ]

    # ── White Balance & Color ─────────────────────────────────────────────
    user_wb = safe(mvsdk.CameraGetUserClrTempGain, h) or (None, None, None)
    wb_win  = safe(mvsdk.CameraGetWbWindow, h) or (None, None, None, None)
    color_params = [
        p("wb_mode",           "WB Mode",            safe(mvsdk.CameraGetWbMode, h)),
        p("clr_temp_mode",     "Color Temp Mode",    safe(mvsdk.CameraGetClrTempMode, h)),
        p("preset_clr_temp",   "Preset Color Temp",  safe(mvsdk.CameraGetPresetClrTemp, h)),
        p("user_wb_r",         "Custom WB R Gain",   user_wb[0]),
        p("user_wb_g",         "Custom WB G Gain",   user_wb[1]),
        p("user_wb_b",         "Custom WB B Gain",   user_wb[2]),
        p("wb_window_h_off",   "WB Window H Offset", wb_win[0],  "px"),
        p("wb_window_v_off",   "WB Window V Offset", wb_win[1],  "px"),
        p("wb_window_width",   "WB Window Width",    wb_win[2],  "px"),
        p("wb_window_height",  "WB Window Height",   wb_win[3],  "px"),
        p("saturation",        "Saturation",         safe(mvsdk.CameraGetSaturation, h)),
        p("saturation_min",    "Saturation Min",     cap.sSaturationRange.iMin if cap else None),
        p("saturation_max",    "Saturation Max",     cap.sSaturationRange.iMax if cap else None),
    ]

    # ── Image Processing ──────────────────────────────────────────────────
    processing_params = [
        p("sharpness",           "Sharpness",               safe(mvsdk.CameraGetSharpness, h)),
        p("sharpness_min",       "Sharpness Min",           cap.sSharpnessRange.iMin if cap else None),
        p("sharpness_max",       "Sharpness Max",           cap.sSharpnessRange.iMax if cap else None),
        p("gamma",               "Gamma",                   safe(mvsdk.CameraGetGamma, h)),
        p("gamma_min",           "Gamma Min",               cap.sGammaRange.iMin     if cap else None),
        p("gamma_max",           "Gamma Max",               cap.sGammaRange.iMax     if cap else None),
        p("contrast",            "Contrast",                safe(mvsdk.CameraGetContrast, h)),
        p("contrast_min",        "Contrast Min",            cap.sContrastRange.iMin  if cap else None),
        p("contrast_max",        "Contrast Max",            cap.sContrastRange.iMax  if cap else None),
        p("noise_filter",        "Noise Filter",            bool(safe(mvsdk.CameraGetNoiseFilterState, h, default=0))),
        p("bayer_dec_algorithm", "Bayer Demosaic Algorithm",safe(mvsdk.CameraGetBayerDecAlgorithm, h, isp_proc)),
        p("correct_dead_pixel",  "Dead Pixel Correction",   bool(safe(mvsdk.CameraGetCorrectDeadPixel, h, default=0))),
        p("lut_mode",            "LUT Mode",                safe(mvsdk.CameraGetLutMode, h)),
        p("lut_preset",          "LUT Preset",              safe(mvsdk.CameraGetLutPresetSel, h)),
        p("hdr",                 "HDR",                     safe(mvsdk.CameraGetHDR, h)),
        p("hdr_gain_mode",       "HDR Gain Mode",           safe(mvsdk.CameraGetHDRGainMode, h)),
    ]

    # ── Orientation ───────────────────────────────────────────────────────
    orientation_params = [
        p("rotation", "Rotation Index", safe(mvsdk.CameraGetRotate, h)),
        p("h_mirror", "Horizontal Mirror", bool(safe(mvsdk.CameraGetMirror, h, 0, default=0))),
        p("v_mirror", "Vertical Mirror",   bool(safe(mvsdk.CameraGetMirror, h, 1, default=0))),
    ]

    # ── Trigger ───────────────────────────────────────────────────────────
    trigger_params = [
        p("trigger_mode",      "Trigger Mode",       safe(mvsdk.CameraGetTriggerMode, h)),
        p("trigger_delay_us",  "Trigger Delay",      safe(mvsdk.CameraGetTriggerDelayTime, h), "µs"),
        p("trigger_count",     "Trigger Count",      safe(mvsdk.CameraGetTriggerCount, h)),
        p("single_grab_mode",  "Single Grab Mode",   bool(safe(mvsdk.CameraGetSingleGrabMode, h, default=0))),
    ]

    # ── External Trigger ──────────────────────────────────────────────────
    ext_trigger_params = [
        p("ext_trig_signal_type",  "Signal Type",         safe(mvsdk.CameraGetExtTrigSignalType, h)),
        p("ext_trig_shutter_type", "Shutter Type",        safe(mvsdk.CameraGetExtTrigShutterType, h)),
        p("ext_trig_delay_us",     "Delay",               safe(mvsdk.CameraGetExtTrigDelayTime, h),    "µs"),
        p("ext_trig_jitter_us",    "Jitter Filter",       safe(mvsdk.CameraGetExtTrigJitterTime, h),   "µs"),
        p("ext_trig_interval_us",  "Min Interval",        safe(mvsdk.CameraGetExtTrigIntervalTime, h), "µs"),
        p("ext_trig_capability",   "Capability Mask",     safe(mvsdk.CameraGetExtTrigCapability, h)),
    ]

    # ── Strobe ────────────────────────────────────────────────────────────
    strobe_params = [
        p("strobe_mode",           "Strobe Mode",         safe(mvsdk.CameraGetStrobeMode, h)),
        p("strobe_polarity",       "Strobe Polarity",     safe(mvsdk.CameraGetStrobePolarity, h)),
        p("strobe_delay_us",       "Strobe Delay",        safe(mvsdk.CameraGetStrobeDelayTime, h),    "µs"),
        p("strobe_pulse_width_us", "Strobe Pulse Width",  safe(mvsdk.CameraGetStrobePulseWidth, h),   "µs"),
    ]

    # ── Transfer ──────────────────────────────────────────────────────────
    transfer_params = [
        p("trans_pack_len",        "Transfer Packet Length", safe(mvsdk.CameraGetTransPackLen, h)),
    ]

    # ── ISP Capabilities (hardware flags) ────────────────────────────────
    isp = cap.sIspCapacity if cap else None
    isp_params = [
        p("mono_sensor",         "Mono Sensor",               bool(isp.bMonoSensor)        if isp else None),
        p("wb_once",             "Manual WB Supported",       bool(isp.bWbOnce)            if isp else None),
        p("auto_wb",             "Auto WB Supported",         bool(isp.bAutoWb)            if isp else None),
        p("auto_exposure",       "Auto Exposure Supported",   bool(isp.bAutoExposure)      if isp else None),
        p("manual_exposure",     "Manual Exposure Supported", bool(isp.bManualExposure)    if isp else None),
        p("anti_flick_support",  "Anti-Flicker Supported",   bool(isp.bAntiFlick)         if isp else None),
        p("device_isp",          "Hardware ISP",              bool(isp.bDeviceIsp)         if isp else None),
        p("force_device_isp",    "Force Hardware ISP",        bool(isp.bForceUseDeviceIsp) if isp else None),
        p("zoom_hd",             "Hardware Zoom",             bool(isp.bZoomHD)            if isp else None),
        p("param_in_device",     "Params Stored In Device",   bool(cap.bParamInDevice)     if cap else None),
    ]

    # ── Statistics ────────────────────────────────────────────────────────
    stat = safe(mvsdk.CameraGetFrameStatistic, h)
    stats_params = [
        p("frames_total",    "Total Frames",    stat.iTotal   if stat else None),
        p("frames_captured", "Captured Frames", stat.iCapture if stat else None),
        p("frames_lost",     "Lost Frames",     stat.iLost    if stat else None),
    ]

    return {
        "camera_id": cam._camera_index,
        "groups": [
            {"id": "device",       "name": "Device",              "params": device_params},
            {"id": "image",        "name": "Image & Resolution",  "params": image_params},
            {"id": "exposure",     "name": "Exposure & AE",       "params": exposure_params},
            {"id": "gain",         "name": "Gain",                "params": gain_params},
            {"id": "color",        "name": "White Balance & Color","params": color_params},
            {"id": "processing",   "name": "Image Processing",    "params": processing_params},
            {"id": "orientation",  "name": "Orientation",         "params": orientation_params},
            {"id": "trigger",      "name": "Trigger",             "params": trigger_params},
            {"id": "ext_trigger",  "name": "External Trigger",    "params": ext_trigger_params},
            {"id": "strobe",       "name": "Strobe",              "params": strobe_params},
            {"id": "transfer",     "name": "Transfer",            "params": transfer_params},
            {"id": "isp_caps",     "name": "ISP Capabilities",    "params": isp_params},
            {"id": "stats",        "name": "Frame Statistics",    "params": stats_params},
        ],
    }


def _make_mjpeg_stream(cam, cam_id: int, fps: float, render_fn, timeout_fn=None):
    """Return a generator function that streams MJPEG frames.

    Increments _calibration_stream_count before returning so callers don't
    need to manage the counter separately.

    render_fn(frame) -> bytes  — encode one frame to JPEG bytes.
    timeout_fn(h_camera, frame_interval) -> (timeout_ms, effective_interval)
        — dynamic grab timeout; if None, uses (1000, frame_interval).
    """
    _calibration_stream_count[cam_id] = _calibration_stream_count.get(cam_id, 0) + 1
    frame_interval = 1.0 / fps

    def generate():
        _continuous_started = False
        try:
            while True:
                loop_start = time.monotonic()

                if cam._h_camera is None:
                    time.sleep(1.0)
                    continue

                if (
                    not cam._streaming
                    and not _continuous_started
                    and cam.mode != CameraMode.HARDWARE_TRIGGER
                ):
                    cam.set_trigger_mode(0)  # preserves AE
                    _continuous_started = True
                    time.sleep(0.1)

                if timeout_fn is not None and cam._h_camera is not None:
                    grab_timeout_ms, effective_interval = timeout_fn(cam._h_camera, frame_interval)
                else:
                    grab_timeout_ms, effective_interval = 1000, frame_interval

                with cam._lock:
                    frame, _head = cam._grab_frame(timeout_ms=grab_timeout_ms)

                if frame is not None:
                    jpeg = render_fn(frame)
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )

                elapsed = time.monotonic() - loop_start
                remaining = effective_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            _calibration_stream_count[cam_id] = _calibration_stream_count.get(cam_id, 1) - 1
            if (
                _continuous_started
                and _calibration_stream_count.get(cam_id, 0) == 0
                and cam._h_camera is not None
                and not cam._streaming
            ):
                try:
                    cam.set_trigger_mode(1)  # preserves AE
                except Exception:
                    pass

    return generate


def create_blueprint(
    cameras: dict[int, MindVisionCamera],
    serial_listener: "SerialTriggerListener | None" = None,
) -> Blueprint:
    bp = Blueprint("mindvision", __name__, url_prefix="/api/cameras")

    from camera.mindvision_trigger import SerialTriggerListener
    if serial_listener is None:
        serial_listener = SerialTriggerListener(cameras)
    _serial_listener = serial_listener

    def _resolve_camera():
        cam_id = request.args.get("camera_id", 0, type=int)
        cam = cameras.get(cam_id)
        return cam, cam_id

    # ── Camera discovery ──────────────────────────────────────────────────────

    @bp.route("", methods=["GET"])
    def list_cameras():
        """Return every known camera with its index and serial number.

        Use this to discover which camera_id corresponds to which physical
        camera (identified by serial_number).  Pass camera_id to all other
        endpoints once you know the mapping.
        """
        return jsonify([
            {
                "camera_id": cam_id,
                "serial_number": cam.serial_number,
                "model": cam.camera_info().get("model"),
                "product_name": cam.camera_info().get("product_name"),
                "port_type": cam.camera_info().get("port_type"),
                "status": "open" if cam.serial_number is not None else "closed",
                "capture_size": cam.camera_info().get("capture_size"),
                "stream_size": cam.camera_info().get("stream_size"),
            }
            for cam_id, cam in sorted(cameras.items())
        ])

    # ── Mode ──────────────────────────────────────────────────────────────────

    @bp.route("/mode", methods=["GET"])
    def get_mode():
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        return jsonify({"camera_id": cam_id, "mode": cam.mode.value})

    @bp.route("/mode", methods=["POST"])
    def set_mode():
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        body = request.get_json(silent=True) or {}
        raw = body.get("mode", "")
        try:
            mode = CameraMode(raw)
        except ValueError:
            valid = [m.value for m in CameraMode]
            return jsonify({"error": f"Invalid mode '{raw}'. Valid: {valid}"}), 400
        try:
            cam.set_mode(mode)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        if mode == CameraMode.HARDWARE_TRIGGER:
            from blueprints.exposure_sync import apply_saved_state_if_enabled
            apply_saved_state_if_enabled(cam, cam_id)
        logger.info("mode_switch_requested", camera_id=cam_id, mode=mode.value)
        return jsonify({"camera_id": cam_id, "mode": mode.value})

    # ── White balance ─────────────────────────────────────────────────────────

    @bp.route("/white-balance", methods=["GET"])
    def get_white_balance():
        import mvsdk
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"calibrated": False})
        auto = mvsdk.CameraGetWbMode(cam._h_camera)
        r, g, b = mvsdk.CameraGetGain(cam._h_camera)
        return jsonify({"calibrated": True, "auto": bool(auto), "r_gain": r, "g_gain": g, "b_gain": b})

    @bp.route("/calibrate-wb", methods=["POST"])
    def calibrate_wb():
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        try:
            gains = cam.calibrate_white_balance()
            return jsonify(gains)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        except Exception:
            logger.exception("calibrate_wb_failed", camera_id=cam_id)
            return jsonify({"error": "Calibration failed"}), 500

    # ── Rotation / mirror ─────────────────────────────────────────────────────

    @bp.route("/orientation", methods=["GET"])
    def get_orientation():
        """Return current rotation and mirror state for a camera.

        Query params:
          camera_id  int  Camera index (default 0)

        Response:
          rotation   int   0=0°, 1=90°CCW, 2=180°, 3=270°CCW
          h_mirror   bool  Horizontal mirror enabled
          v_mirror   bool  Vertical mirror enabled
        """
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404
        try:
            orientation = cam.get_orientation()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        return jsonify({"camera_id": cam_id, **orientation})

    @bp.route("/rotation", methods=["POST"])
    def set_rotation():
        """Set SDK rotation for a camera and persist to device config.

        JSON body:
          rotation  int  0=0°, 1=90°CCW, 2=180°, 3=270°CCW

        Query params:
          camera_id  int  Camera index (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        body = request.get_json(silent=True) or {}
        rotation = body.get("rotation")
        if rotation is None or rotation not in (0, 1, 2, 3):
            return jsonify({"error": "rotation must be 0, 1, 2, or 3"}), 400
        try:
            cam.set_rotation(rotation)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        logger.info("rotation_set", camera_id=cam_id, rotation=rotation)
        return jsonify({"camera_id": cam_id, "rotation": rotation})

    @bp.route("/mirror", methods=["POST"])
    def set_mirror():
        """Set SDK mirror for a camera and persist to device config.

        JSON body:
          direction  str  "horizontal" or "vertical"
          enable     bool

        Query params:
          camera_id  int  Camera index (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        body = request.get_json(silent=True) or {}
        direction_str = body.get("direction", "")
        enable = body.get("enable")
        if direction_str not in ("horizontal", "vertical"):
            return jsonify({"error": "direction must be 'horizontal' or 'vertical'"}), 400
        if not isinstance(enable, bool):
            return jsonify({"error": "enable must be a boolean"}), 400
        direction = 0 if direction_str == "horizontal" else 1
        try:
            cam.set_mirror(direction, enable)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        logger.info("mirror_set", camera_id=cam_id, direction=direction_str, enabled=enable)
        return jsonify({"camera_id": cam_id, "direction": direction_str, "enable": enable})

    # ── Multi-camera capture ───────────────────────────────────────────────────

    @bp.route("/capture-all", methods=["POST"])
    def capture_all():
        """Capture one frame from every camera simultaneously and return a zip.

        For hardware-trigger mode all grab threads block together — the physical
        signal releases them all at once.  For continuous/capture mode each
        thread grabs the next available frame independently.
        """
        results: dict[int, Path] = {}
        errors: dict[int, str] = {}
        mu = threading.Lock()

        config.CAPTURE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=config.CAPTURE_TMP_DIR))

        def grab_one(cam_id: int, cam: MindVisionCamera) -> None:
            try:
                path, _ = cam.capture_image(output_folder=tmp_dir)
                with mu:
                    results[cam_id] = path
            except Exception as e:
                with mu:
                    errors[cam_id] = str(e)

        cam_ids = list(cameras.keys())
        threads = [
            threading.Thread(target=grab_one, args=(cam_id, cameras[cam_id]), daemon=True)
            for cam_id in cam_ids
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        timed_out = [cam_id for cam_id, t in zip(cam_ids, threads) if t.is_alive()]
        if timed_out:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.warning("capture_all_timed_out", camera_ids=timed_out)
            return jsonify({
                "error": "Capture timed out waiting for camera(s)",
                "details": {str(k): "timed out after 15 s" for k in timed_out},
            }), 504

        if errors:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.warning("capture_all_partial_failure", errors={str(k): v for k, v in errors.items()})
            return jsonify({
                "error": "One or more cameras failed to capture",
                "details": {str(k): v for k, v in errors.items()},
            }), 500

        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
                for cam_id in sorted(results):
                    zf.write(results[cam_id], f"camera_{cam_id}.jpg")
            buf.seek(0)

            @after_this_request
            def cleanup(response):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return response

            logger.info("capture_all_complete", camera_ids=sorted(results))
            return send_file(
                buf,
                mimetype="application/zip",
                as_attachment=True,
                download_name="capture_all.zip",
            )
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.exception("capture_all_zip_failed")
            return jsonify({"error": "Failed to build capture archive"}), 500

    # ── Calibration stream ────────────────────────────────────────────────────

    @bp.route("/calibration/stream")
    def calibration_stream():
        """MJPEG stream with focus peaking, sharpness score, and exposure overlay.

        Query params:
          camera_id       int   Camera index (default 0)
          fps             float Frames per second (default 2, max 10)
          peak_threshold  int   Gradient threshold for peaking highlights (default 50)
          max_width       int   Downscale width before overlay (default 1280)
          charuco         int   1 to enable ChArUco overlay (default 0)
          clip_highlight  int   0 to hide overexposure (red) highlight (default 1)
          show_overlay    int   0 for clean raw frame with no overlay at all (default 1)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        if isinstance(cam, MindVisionCamera) and cam.mode == CameraMode.HARDWARE_TRIGGER:
            return Response("Camera is in hardware trigger mode", status=409, mimetype="text/plain")

        fps = max(0.5, min(request.args.get("fps", 2.0, type=float), 10.0))
        peak_threshold = request.args.get("peak_threshold", 50, type=int)
        max_width = request.args.get("max_width", 1280, type=int)
        detect_charuco = request.args.get("charuco", 0, type=int) == 1
        clip_highlight = request.args.get("clip_highlight", 1, type=int) == 1
        show_overlay = request.args.get("show_overlay", 1, type=int) == 1

        if cam_id not in _calibration_history:
            _calibration_history[cam_id] = deque(maxlen=30)
        history = _calibration_history[cam_id]

        # Each new connection = fresh session: clear peak so the bar restarts.
        _peak_scores.pop(cam_id, None)
        history.clear()

        def _render_cal(frame):
            if show_overlay:
                return _render_calibration_overlay(
                    frame, history, peak_threshold, max_width,
                    cam_id, detect_charuco, clip_highlight,
                )
            return _encode_raw_frame(frame, max_width)

        return Response(
            _make_mjpeg_stream(cam, cam_id, fps, _render_cal)(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    # ── Lens placement stream (lean, no focus peaking) ───────────────────────

    @bp.route("/lens/stream")
    def lens_stream():
        """Lean MJPEG stream for ChArUco board placement during lens calibration.

        No sharpness computation or focus peaking — just the raw frame with a
        guide box and ChArUco corner overlay.

        Query params:
          camera_id   int   Camera index (default 0)
          fps         float Frames per second (default 5, max 10)
          guide_pct   int   Guide box size as % of the shorter frame dimension (default 40)
          max_width   int   Downscale width (default 1280)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        if isinstance(cam, MindVisionCamera) and cam.mode == CameraMode.HARDWARE_TRIGGER:
            return Response("Camera is in hardware trigger mode", status=409, mimetype="text/plain")

        fps = max(0.5, min(request.args.get("fps", 2.0, type=float), 10.0))
        guide_pct = max(10, min(90, request.args.get("guide_pct", 40, type=int)))
        max_width = request.args.get("max_width", 960, type=int)
        cx_frac = max(0.05, min(0.95, request.args.get("cx", 0.5, type=float)))
        cy_frac = max(0.05, min(0.95, request.args.get("cy", 0.5, type=float)))

        def _render_lens(frame):
            return _render_lens_stream_frame(frame, max_width, guide_pct, cam_id, cx_frac, cy_frac)

        return Response(
            _make_mjpeg_stream(cam, cam_id, fps, _render_lens)(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    # ── Per-camera settings (read / apply / save / factory-reset / stream) ───

    @bp.route("/settings", methods=["GET"])
    def get_settings():
        """Return all tunable SDK settings and their valid ranges for a camera."""
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404
        s = _read_mv_settings(cam._h_camera, cam._cap)
        s["camera_id"] = cam_id
        return jsonify(s)

    @bp.route("/settings", methods=["POST"])
    def apply_settings():
        """Apply settings to the camera hardware without persisting.

        Send any subset of the writable fields: ae_enabled, exposure_us, ae_target,
        analog_gain, r_gain, g_gain, b_gain, sharpness, gamma, rotation, h_mirror, v_mirror.
        Changes are live immediately but lost on camera restart unless followed by /settings/save.
        """
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404
        body = request.get_json(silent=True) or {}
        applied, errors = _apply_mv_settings(cam._h_camera, body)
        status = 207 if errors else 200
        return jsonify({"camera_id": cam_id, "applied": applied, "errors": errors}), status

    @bp.route("/settings/save", methods=["POST"])
    def save_settings():
        """Apply settings and persist them to the SDK's per-serial config file."""
        import mvsdk
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404
        body = request.get_json(silent=True) or {}
        applied, errors = _apply_mv_settings(cam._h_camera, body)
        if applied:
            mvsdk.CameraSaveParameter(cam._h_camera, 0)
            logger.info("camera_settings_saved", camera_id=cam_id, keys=applied)
        return jsonify({"camera_id": cam_id, "applied": applied, "errors": errors,
                        "saved": bool(applied)}), (207 if errors else 200)

    @bp.route("/settings/factory-reset", methods=["POST"])
    def factory_reset_settings():
        """Reset camera parameters to factory defaults and persist.

        Only camera SDK parameters are affected. Lens distortion and stitch
        calibration (stored in separate JSON files) are untouched.
        """
        import mvsdk
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404
        h = cam._h_camera
        cap = cam._cap
        defaults = {
            "ae_enabled": True,
            "ae_target": 100,
            "analog_gain": cap.sExposeDesc.uiAnalogGainMin if cap else 16,
            "r_gain": 100, "g_gain": 100, "b_gain": 100,
            "sharpness": cap.sSharpnessRange.iMin if cap else 0,
            "gamma": 100,
            "rotation": 0,
            "h_mirror": False,
            "v_mirror": False,
        }
        applied, errors = _apply_mv_settings(h, defaults)
        if not errors:
            mvsdk.CameraSaveParameter(h, 0)
            logger.info("camera_factory_reset", camera_id=cam_id)
        return jsonify({"camera_id": cam_id, "applied": applied, "errors": errors,
                        "saved": not bool(errors)}), (207 if errors else 200)

    @bp.route("/settings/stream")
    def settings_stream():
        """Clean MJPEG stream with no overlay for the settings preview panel.

        Query params:
          camera_id  int    Camera index (default 0)
          fps        float  Frames per second (default 5, max 10)
          max_width  int    Downscale width (default 1280)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404
        if isinstance(cam, MindVisionCamera) and cam.mode == CameraMode.HARDWARE_TRIGGER:
            return Response("Camera is in hardware trigger mode", status=409, mimetype="text/plain")

        fps = max(0.5, min(request.args.get("fps", 5.0, type=float), 10.0))
        max_width = request.args.get("max_width", 1280, type=int)

        def _settings_timeout(h_camera, frame_interval):
            import mvsdk
            try:
                exp_us = mvsdk.CameraGetExposureTime(h_camera)
            except Exception:
                exp_us = 0.0
            # Pad the grab timeout so a long exposure doesn't kill the stream.
            return cam.exposure_grab_timeout_ms(), max(frame_interval, exp_us / 1_000_000)

        def _render_settings(frame):
            return _encode_raw_frame(frame, max_width, quality=80)

        return Response(
            _make_mjpeg_stream(cam, cam_id, fps, _render_settings, timeout_fn=_settings_timeout)(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @bp.route("/settings/snapshot")
    def settings_snapshot():
        """Grab a single frame and return it as JPEG — works at any exposure time.

        Query params:
          camera_id  int  Camera index (default 0)
          max_width  int  Downscale width (default 1280)
        """
        import mvsdk as _mvsdk

        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404

        max_width = request.args.get("max_width", 1280, type=int)

        try:
            with cam._lock:
                if not cam._streaming and cam.mode != CameraMode.HARDWARE_TRIGGER:
                    _mvsdk.CameraSoftTrigger(cam._h_camera)
                frame, _head = cam._grab_frame(timeout_ms=cam.exposure_grab_timeout_ms())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 503

        if frame is None:
            return jsonify({"error": "No frame available — camera may still be exposing"}), 503

        jpeg = _encode_raw_frame(frame, max_width, quality=90)
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @bp.route("/calibration/score")
    def calibration_score():
        """Return the current sharpness score and trend as JSON (single frame).

        Useful for scripted calibration loops. Uses the same rolling history as
        the calibration stream, so calling both simultaneously gives consistent trends.

        Query params:
          camera_id  int  Camera index (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None:
            return jsonify({"error": f"Camera {cam_id} not found"}), 404

        if cam_id not in _calibration_history:
            _calibration_history[cam_id] = deque(maxlen=30)
        history = _calibration_history[cam_id]

        try:
            import mvsdk
            import numpy as np

            with cam._lock:
                if not cam._streaming:
                    mvsdk.CameraSoftTrigger(cam._h_camera)
                frame, _head = cam._grab_frame()
        except Exception as exc:
            return jsonify({"error": str(exc)}), 503

        if frame is None:
            return jsonify({"error": "No frame available"}), 503

        h, w = frame.shape[:2]
        if frame.ndim == 3 and frame.shape[2] == 3:
            gray = (
                0.114 * frame[:, :, 0] + 0.587 * frame[:, :, 1] + 0.299 * frame[:, :, 2]
            ).astype(np.float32)
        else:
            gray = frame[:, :, 0].astype(np.float32) if frame.ndim == 3 else frame.astype(np.float32)

        roi_y1, roi_y2 = h // 3, 2 * h // 3
        roi_x1, roi_x2 = w // 3, 2 * w // 3
        roi = gray[roi_y1:roi_y2, roi_x1:roi_x2]
        lap = (
            roi[:-2, 1:-1] + roi[2:, 1:-1] + roi[1:-1, :-2] + roi[1:-1, 2:]
            - 4.0 * roi[1:-1, 1:-1]
        )
        score = float(np.var(lap))
        history.append(score)

        trend = "measuring"
        suggestion = "keep adjusting and watch the score"
        if len(history) >= 6:
            arr = list(history)
            mid = len(arr) // 2
            first_mean = float(np.mean(arr[:mid]))
            last_mean = float(np.mean(arr[mid:]))
            rel = (last_mean - first_mean) / max(abs(first_mean), 1.0)
            if rel > 0.05:
                trend, suggestion = "increasing", "keep going"
            elif rel < -0.05:
                trend, suggestion = "decreasing", "reverse direction"
            else:
                trend, suggestion = "stable", "at or near peak focus"

        session_peak = max(_peak_scores.get(cam_id, 0.0), score)
        _peak_scores[cam_id] = session_peak
        pct_of_peak = round(score / max(session_peak, 1.0) * 100, 1)

        return jsonify({
            "camera_id": cam_id,
            "score": round(score, 2),
            "session_peak": round(session_peak, 2),
            "pct_of_peak": pct_of_peak,
            "trend": trend,
            "suggestion": suggestion,
            "history_length": len(history),
            "roi": {"x1": roi_x1, "y1": roi_y1, "x2": roi_x2, "y2": roi_y2},
        })

    # ── Camera config (read-only) ─────────────────────────────────────────────

    def _all_cameras_full_config() -> list[dict]:
        result = []
        for cam_id, cam in sorted(cameras.items()):
            if cam._h_camera is not None:
                result.append(_read_full_config(cam._h_camera, cam))
            else:
                result.append({
                    "camera_id": cam_id,
                    "groups": [],
                    "status": "closed",
                })
        return result

    @bp.route("/config/full", methods=["GET"])
    def get_full_config():
        """Return every SDK parameter grouped by category for one camera.

        Query params:
          camera_id  int  Camera index (default 0)
        """
        cam, cam_id = _resolve_camera()
        if cam is None or cam._h_camera is None:
            return jsonify({"error": f"Camera {cam_id} not found or not open"}), 404
        return jsonify(_read_full_config(cam._h_camera, cam))

    @bp.route("/config/all", methods=["GET"])
    def get_all_config():
        """Return full grouped config for every connected camera."""
        return jsonify(_all_cameras_full_config())

    @bp.route("/config/download", methods=["GET"])
    def download_all_config():
        """Download full config for every camera as a JSON file."""
        import json
        from datetime import datetime, timezone
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cameras": _all_cameras_full_config(),
        }
        payload = json.dumps(data, indent=2).encode()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return Response(
            payload,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=camera_config_{ts}.json"},
        )

    return bp
