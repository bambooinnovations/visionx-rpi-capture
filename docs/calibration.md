# Calibration

This page covers calibration procedures for MindVision cameras: white balance, focus, orientation, and multi-camera stitch.

> **Fabric station users:** use the fabric-station-specific guide at [frabic-station/calibration.md](frabic-station/calibration.md). It includes machine-specific prerequisites (roll loading, HW trigger disable) and step-by-step UI walkthroughs with screenshots.

---

## White balance

Point the camera at a neutral grey or white surface under your working lights, then run one-shot calibration:

```bash
curl -X POST http://localhost:8080/api/cameras/calibrate-wb?camera_id=0
```

Gains are persisted to `calibration.json` and applied automatically on every subsequent camera open.

Check the current state at any time:

```bash
curl http://localhost:8080/api/cameras/white-balance?camera_id=0
```

---

## Focus

Use the calibration stream for live sharpness feedback while adjusting the lens focus ring:

```
GET /api/cameras/calibration/stream?camera_id=0
```

Open it in a browser or any MJPEG viewer. The overlay shows:

- **Magenta pixels** — focus peaking (high edge-contrast areas)
- **Sharpness score and trend** (top-left) — Laplacian variance with increasing / stable / decreasing feedback
- **Sharpness bar** — normalised to session peak

Adjust the lens focus ring until the score peaks and the trend shows "at or near peak", then lock the focus ring.

Supported query parameters: `fps`, `peak_threshold`, `max_width`, `charuco`, `clip_highlight`, `show_overlay` — see [api.md](api.md#get-apicamerascalibrationstream).

---

## Orientation

```bash
# Check current state
curl http://localhost:8080/api/cameras/orientation?camera_id=0

# Set rotation (0 = none, 1 = 90° CCW, 2 = 180°, 3 = 270° CCW)
curl -X POST http://localhost:8080/api/cameras/rotation?camera_id=0 \
     -H 'Content-Type: application/json' \
     -d '{"rotation": 2}'

# Set mirror
curl -X POST http://localhost:8080/api/cameras/mirror?camera_id=0 \
     -H 'Content-Type: application/json' \
     -d '{"direction": "horizontal", "enable": true}'
```

Changes are applied at the SDK ISP level and persisted to the per-serial device config.

---

## Lens distortion calibration

Fits camera intrinsics (K, D) from ChArUco frames captured at 15–20 different board positions. Required for high geometric accuracy; also used automatically by the stitch pipeline to undistort frames before homography fitting.

1. Collect frames — hold the ChArUco board at different angles and distances, call `POST /api/lens/collect` for each position.
2. Compute — call `POST /api/lens/compute` once you have ≥ 10 accepted frames.
3. Re-run stitch calibration afterwards so homographies are refitted to undistorted images.

See [api.md](api.md#lens-calibration) for full endpoint reference.

---

## Stitch calibration (multi-camera)

Computes a homography for each camera that maps it onto a shared flat canvas, using a ChArUco board placed in the overlap zone between cameras.

```bash
# Calibrate cameras 0 and 1
curl -X POST http://localhost:8080/api/stitch/calibrate \
     -H 'Content-Type: application/json' \
     -d '{"cameras": [0, 1]}'

# Check status
curl http://localhost:8080/api/stitch/calibrate
```

After stitch calibration, run colour correction to equalise per-camera colour balance:

```bash
curl -X POST http://localhost:8080/api/stitch/calibrate-color
```

See [api.md](api.md#stitch) for full endpoint reference including 3-camera non-overlapping setups.

---

## Exposure sync (experimental)

Fixes AE instability on cameras whose framing is entirely fabric with no fixed background to meter
against — see `/exposure-sync` in the app for the full guided walkthrough (reference camera picker,
live view, capture/nudge/apply/preview/save). The page itself explains each step; this section is just
the API-level summary for scripting or debugging.

One camera (the "reference") keeps running independent auto-exposure. The others are locked to a
manually-set `exposure_us`/`analog_gain` matching whatever the reference converges to. This is a
one-time calibration, not continuous re-sync — the saved values are (re-)applied to follower cameras
each time they enter hardware-trigger mode, not on every trigger.

```bash
# Current state
curl http://localhost:8080/api/exposure-sync/state

# Pick a reference camera
curl -X POST http://localhost:8080/api/exposure-sync/reference -d '{"camera_id": 1}' -H 'Content-Type: application/json'

# Enable (line must be stopped first)
curl -X POST http://localhost:8080/api/exposure-sync/enabled -d '{"enabled": true}' -H 'Content-Type: application/json'
```

`POST /apply` writes to the follower cameras' live SDK handles but does **not** persist — only
`POST /save` calls `CameraSaveParameter` and writes `experimental.exposure_sync_*` to
`runtime_config.json`, so the calibration survives a camera reopen/restart.
