# Calibration Guide

Covers all calibration procedures for MindVision camera setups: white balance, focus, orientation, and multi-camera stitch.

---

## White balance

Point the camera at a neutral white or grey surface under your working light, then run one-push calibration:

```bash
curl -X POST http://localhost:8080/rpi/mindvision/calibrate-wb
# {"r_gain": 112, "g_gain": 100, "b_gain": 138, "calibrated_at": "..."}
```

Gains are stored in `calibration.json` and applied automatically on every subsequent camera open or stream start.

```bash
# Inspect stored gains
curl http://localhost:8080/rpi/mindvision/white-balance
```

---

## Focus

MindVision lenses have a manual focus ring. The focus calibration stream lets you dial in focus precisely using a live overlay.

### 1. Print the calibration target

Generate a **Siemens star** — the industry standard for focus and resolution testing:

```bash
.venv/bin/python scripts/gen_siemens_star.py
# Saved: siemens_star_letter.png  (2550×3300 px, 300 DPI, letter paper)
```

Print at 100% scale (no "fit to page") on letter paper. Place it flat, perpendicular to the camera, at your intended working distance.

### 2. Open the focus stream

```
http://<rpi-ip>:8080/rpi/mindvision/calibration/stream
```

| Overlay element              | What it means                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| **Magenta pixels**           | Focus peaking — pixels with high edge contrast. More magenta = sharper.              |
| **Yellow ROI box**           | The centre-third region used for the sharpness score. Keep the star inside this box. |
| **Score / trend** (top-left) | Laplacian variance of the ROI and rolling trend.                                     |
| **Exposure** (top-left)      | Clipped pixel percentage and suggestion.                                             |
| **Sharpness bar** (bottom)   | Relative score normalised to the highest value seen this session.                    |

### 3. Adjust focus

Turn the focus ring slowly and watch the trend:

| Indicator               | Meaning                            |
| ----------------------- | ---------------------------------- |
| **↑ keep going**        | Sharpness improving — keep turning |
| **↓ reverse direction** | Passed peak — back off slightly    |
| **● at or near peak**   | At or very close to optimal focus  |

When the trend stabilises at `●` with a full green bar, lock the focus ring.

### Query parameters

| Parameter        | Default | Description                                                                                                        |
| ---------------- | ------- | ------------------------------------------------------------------------------------------------------------------ |
| `camera_id`      | `0`     | Camera to use                                                                                                      |
| `fps`            | `2`     | Stream frame rate (0.5–10)                                                                                         |
| `peak_threshold` | `50`    | Gradient cutoff for peaking highlights. Lower = more pixels highlighted; raise if the whole image turns magenta.   |
| `max_width`      | `1280`  | Downscale frames to this width before processing. Reduces CPU load.                                                |
| `charuco`        | `0`     | Set to `1` to enable ChArUco board detection overlay (see [Stitch calibration](#stitch-calibration-multi-camera)). |

Example:

```
http://<rpi-ip>:8080/rpi/mindvision/calibration/stream?fps=3&peak_threshold=70&charuco=1
```

### JSON score endpoint

For scripted loops, poll the score directly:

```bash
curl http://localhost:8080/rpi/mindvision/calibration/score
```

```json
{
  "camera_id": 0,
  "score": 1842.3,
  "trend": "increasing",
  "suggestion": "keep going",
  "history_length": 12,
  "roi": { "x1": 427, "y1": 320, "x2": 853, "y2": 640 }
}
```

Use `trend` and `suggestion` for direction guidance. The raw `score` is scene-dependent and not comparable across cameras or scenes.

---

## Camera orientation

If a camera is mounted upside-down or sideways, correct it via the orientation endpoints. Settings are applied at the SDK ISP level and persisted so they survive restarts.

```bash
# Read current state
curl http://localhost:8080/rpi/mindvision/orientation
# {"camera_id": 0, "rotation": 0, "h_mirror": false, "v_mirror": false}

# Set rotation
curl -X POST http://localhost:8080/rpi/mindvision/rotation \
     -H 'Content-Type: application/json' \
     -d '{"rotation": 2}'

# Set mirror
curl -X POST http://localhost:8080/rpi/mindvision/mirror \
     -H 'Content-Type: application/json' \
     -d '{"direction": "horizontal", "enable": true}'
```

| `rotation` | Effect                 |
| ---------- | ---------------------- |
| `0`        | No rotation            |
| `1`        | 90° counter-clockwise  |
| `2`        | 180°                   |
| `3`        | 270° counter-clockwise |

| `direction`    | Effect            |
| -------------- | ----------------- |
| `"horizontal"` | Flip left ↔ right |
| `"vertical"`   | Flip top ↔ bottom |

Both endpoints accept `?camera_id=N`.

---

## Lens distortion calibration

Wide-angle and short-focal-length lenses introduce barrel or pincushion distortion. Correcting it before stitching improves homography accuracy and removes bent straight lines from the output. The lens calibration workflow uses the same **ChArUco board** as stitch calibration to fit a camera matrix **K** and distortion coefficients **D** for each camera.

**Board file:** [`charuco/charuco_20x14_10mm_checker_8mm_marker_DICT_4X4_250_40px.png`](../charuco/charuco_20x14_10mm_checker_8mm_marker_DICT_4X4_250_40px.png)

### Overview

1. **Collect** 15–20 frames with the board in varied positions, angles, and distances.
2. **Compute** — the server fits K and D and stores them in `data/lens_calibration.json`.
3. Any pipeline that needs undistorted frames (e.g. stitching) reads K/D automatically via `get_camera_intrinsics(cam_id)`.

After computing new lens calibration, **re-run stitch calibration** so homographies are refitted to undistorted images.

### Endpoints

| Method | Path                              | Description                                          |
| ------ | --------------------------------- | ---------------------------------------------------- |
| GET    | `/rpi/mindvision/lens`            | Status — calibrated flag, RMS, buffer frame count    |
| POST   | `/rpi/mindvision/lens/collect`    | Grab one frame, detect corners, add to buffer        |
| POST   | `/rpi/mindvision/lens/compute`    | Fit K and D from the buffer and save                 |
| DELETE | `/rpi/mindvision/lens`            | Clear stored calibration and/or the collection buffer|

### Step 1 — Check status

```bash
curl http://localhost:8080/rpi/mindvision/lens
```

```json
{
  "0": { "calibrated": false, "rms": null, "frames_used": null, "calibrated_at": null, "buffered_frames": 0 },
  "1": { "calibrated": false, "rms": null, "frames_used": null, "calibrated_at": null, "buffered_frames": 0 }
}
```

### Step 2 — Collect frames

Hold the board at a new position/angle/distance and call:

```bash
curl -X POST http://localhost:8080/rpi/mindvision/lens/collect \
     -H 'Content-Type: application/json' \
     -d '{"camera_id": 0}'
```

```json
{
  "accepted": true,
  "corners_detected": 182,
  "buffered_frames": 7,
  "hint": "Need 8 more frames — keep collecting"
}
```

Repeat from **15–20 varied positions** per camera. Vary tilt, rotation, and distance — do not keep the board in the same plane for every shot. If the board is rejected (`accepted: false`), reposition it and try again.

| `accepted` | Reason                                                                       |
| ---------- | ---------------------------------------------------------------------------- |
| `true`     | ≥ 6 ChArUco corners detected — frame added to buffer                         |
| `false`    | Fewer than 6 corners — board too far, bad lighting, or partially out of frame|

#### POST /collect parameters

| Parameter    | Type  | Default       | Description                                      |
| ------------ | ----- | ------------- | ------------------------------------------------ |
| `camera_id`  | int   | `0`           | Camera to grab from                              |
| `board_cols` | int   | `20`          | Board column count                               |
| `board_rows` | int   | `14`          | Board row count                                  |
| `square_mm`  | float | `10.0`        | Checker square size in mm                        |
| `marker_mm`  | float | `8.0`         | ArUco marker size in mm                          |
| `aruco_dict` | str   | `DICT_4X4_250`| ArUco dictionary name                            |

### Step 3 — Compute calibration

Once at least 15 frames are buffered (minimum 10 accepted):

```bash
curl -X POST http://localhost:8080/rpi/mindvision/lens/compute \
     -H 'Content-Type: application/json' \
     -d '{"camera_id": 0}'
```

```json
{
  "camera_id": 0,
  "rms": 0.42,
  "frames_used": 17,
  "hint": "Re-run POST /rpi/mindvision/stitch/calibrate to refit homographies on undistorted images"
}
```

`rms` is the RMS reprojection error in pixels. Values under **1.0** are acceptable; under **0.5** is good. Higher values suggest the board was not varied enough or some frames had motion blur — clear the buffer and recollect.

Results are stored per-camera in `data/lens_calibration.json`. Running compute again for the same camera overwrites that camera's entry only.

### Step 4 — Refit stitch calibration

Lens correction is applied before homography computation. After updating lens calibration, rerun stitch calibration so the homographies reflect undistorted images:

```bash
curl -X POST http://localhost:8080/rpi/mindvision/stitch/calibrate \
     -H 'Content-Type: application/json' \
     -d '{"cameras": [0, 1]}'
```

### Clearing calibration

```bash
# Clear calibration and buffer for one camera
curl -X DELETE http://localhost:8080/rpi/mindvision/lens \
     -H 'Content-Type: application/json' \
     -d '{"camera_id": 0}'

# Clear everything (all cameras)
curl -X DELETE http://localhost:8080/rpi/mindvision/lens
```

To keep the buffer but delete only the stored K/D (e.g. to recompute from existing frames):

```bash
curl -X DELETE http://localhost:8080/rpi/mindvision/lens \
     -H 'Content-Type: application/json' \
     -d '{"camera_id": 0, "buffer": false}'
```

---

## Stitch calibration (multi-camera)

Multi-camera stitching uses a **ChArUco board** to compute a homography for each camera — a transform that maps each camera's pixel coordinates to a shared flat coordinate plane (the physical surface of the board). Once calibrated, all cameras are expressed in the same coordinate space and can be composited into a single image.

**Board file:** [`charuco/charuco_20x14_10mm_checker_8mm_marker_DICT_4X4_250_40px.png`](../charuco/charuco_20x14_10mm_checker_8mm_marker_DICT_4X4_250_40px.png)

- 20 × 14 squares, 10 mm checker size, 8 mm ArUco marker size, DICT_4X4_250

Print at 100% scale (no "fit to page"). The filename encodes the physical dimensions — at 40 px/square and 10 mm/square, the image is pre-sized so 1 pixel = 0.25 mm at print time. Measure a square on the printout and confirm it is 10 mm; if not, your printer scaled the image.

ChArUco combines checkerboard precision with ArUco robustness — partial occlusion or a camera seeing only part of the board still produces a valid calibration as long as enough corners are detected.

> **Rotation/misalignment:** The homography is a full perspective transform, so small physical camera rotations or tilts are corrected automatically. No manual alignment is needed.

### Calibration file

Homographies are saved to `data/stitch_calibration.json`. Each camera has its own entry; running calibration again for a camera updates only that entry and leaves others untouched.

### Endpoints

| Method | Path                               | Description                                                          |
| ------ | ---------------------------------- | -------------------------------------------------------------------- |
| POST   | `/rpi/mindvision/stitch/calibrate` | Capture specified cameras, detect board, save homographies           |
| GET    | `/rpi/mindvision/stitch/calibrate` | Return calibration status and per-camera corner counts               |
| DELETE | `/rpi/mindvision/stitch/calibrate` | Clear the calibration file and start over                            |
| GET    | `/rpi/mindvision/stitch/preview`   | Capture all cameras, return a single stitched JPEG                   |
| GET    | `/rpi/mindvision/stitch/stream`    | MJPEG stream — stitched if fully calibrated, single camera otherwise |

### 2-camera setup

Both cameras share a direct field of view. One calibration pass is enough:

```bash
# 1. Place board where both cameras can see it
curl -X POST http://localhost:8080/rpi/mindvision/stitch/calibrate \
     -H 'Content-Type: application/json' \
     -d '{"cameras": [0, 1]}'
```

### 3-camera setup (non-overlapping outer cameras)

When the left and right cameras do not share a common field of view, calibrate in two passes using the center camera as the bridge. The center camera's homography (computed in the first pass) anchors all three into the same coordinate space.

```
[cam0] ←─ both see board ─→ [cam1] ←─ both see board ─→ [cam2]
```

```bash
# Pass 1: board between cam0 and cam1
curl -X POST http://localhost:8080/rpi/mindvision/stitch/calibrate \
     -H 'Content-Type: application/json' \
     -d '{"cameras": [0, 1]}'

# Move the board between cam1 and cam2
# Pass 2: board between cam1 and cam2
curl -X POST http://localhost:8080/rpi/mindvision/stitch/calibrate \
     -H 'Content-Type: application/json' \
     -d '{"cameras": [1, 2]}'
```

Each pass merges into the same file. Cam1 is recalibrated in pass 2 (which is fine — it refines it) and its homography from pass 1 is replaced.

### Verify calibration status

```bash
curl http://localhost:8080/rpi/mindvision/stitch/calibrate
```

```json
{
  "calibrated": true,
  "cameras_calibrated": [0, 1, 2],
  "cameras_missing": [],
  "ready_to_stitch": true,
  "px_per_mm": 10.0,
  "canvas": { "width": 2000, "height": 1400 },
  "cameras": {
    "0": {
      "corners_detected": 156,
      "inliers": 148,
      "calibrated_at": "2026-05-25T..."
    },
    "1": {
      "corners_detected": 203,
      "inliers": 198,
      "calibrated_at": "2026-05-25T..."
    },
    "2": {
      "corners_detected": 141,
      "inliers": 135,
      "calibrated_at": "2026-05-25T..."
    }
  }
}
```

`corners_detected` is the raw count from the ChArUco interpolation. `inliers` is the RANSAC-verified count used for the homography — a high inlier ratio (>90%) indicates a good fit.

### Verify board detection before calibrating

Enable the ChArUco overlay on the calibration stream to confirm the board is visible and the corner count is sufficient before running the calibration endpoint:

```
http://<rpi-ip>:8080/rpi/mindvision/calibration/stream?camera_id=0&charuco=1
```

The overlay line colour indicates readiness:

| Colour | Meaning                                                              |
| ------ | -------------------------------------------------------------------- |
| Green  | ≥ 8 corners detected — ready to calibrate                            |
| Orange | Corners detected but too few — move board closer or improve lighting |
| Red    | Not detected — board not visible or too small in frame               |

Aim for at least 20–30 corners per camera for a reliable homography.

### Preview the stitch

```bash
curl http://localhost:8080/rpi/mindvision/stitch/preview --output stitch.jpg
```

Returns 412 with a hint if calibration is incomplete:

```json
{
  "error": "Calibration incomplete — cameras [2] not yet calibrated",
  "cameras_calibrated": [0, 1],
  "cameras_missing": [2],
  "hint": "POST /rpi/mindvision/stitch/calibrate with {\"cameras\": [2]}"
}
```

### POST /calibrate parameters

| Parameter     | Type      | Default     | Description                                                                                         |
| ------------- | --------- | ----------- | --------------------------------------------------------------------------------------------------- |
| `cameras`     | list[int] | all cameras | Camera IDs to calibrate in this pass, e.g. `[0, 1]`                                                 |
| `px_per_mm`   | float     | `10.0`      | Canvas resolution. 10 px/mm → 1 mm of board = 10 output pixels. Must be the same across all passes. |
| `min_corners` | int       | `8`         | Minimum ChArUco corners required per camera to accept the result.                                   |

### GET /stitch/stream parameters

| Parameter   | Type  | Default | Description                            |
| ----------- | ----- | ------- | -------------------------------------- |
| `fps`       | float | `1.0`   | Frame rate (0.1–5)                     |
| `quality`   | int   | `75`    | JPEG quality (1–100)                   |
| `camera_id` | int   | `0`     | Fallback camera ID when not calibrated |

### Clearing and recalibrating

If cameras are repositioned or calibration drift is suspected, clear and start over:

```bash
curl -X DELETE http://localhost:8080/rpi/mindvision/stitch/calibrate
```
