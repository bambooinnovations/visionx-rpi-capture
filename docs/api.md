# API Reference

All endpoints are served on port **8080**. MindVision-specific endpoints are only registered when `camera.type = "mindvision"` in `configuration.toml`.

---

## Core

### `GET /api/health`

Returns `{"status": "ok"}`. Use for uptime monitoring.

---

### `GET /api/metrics/stats`

Returns capture performance statistics from the SQLite metrics database.

---

### `GET /rpi/stream`

MJPEG live preview stream. Behaviour depends on whether `camera_id` is supplied and how many cameras are connected:

- **`camera_id` supplied** — streams that specific camera regardless of calibration state.
- **No `camera_id`, multiple cameras** — redirects to [`GET /api/stitch/stream`](#get-apistitch-stream): stitched composite if fully calibrated, single-camera fallback otherwise.
- **No `camera_id`, single camera** — streams camera `0` directly (no redirect).

For MindVision cameras, returns `409` if the camera is in `hardware_trigger` mode. Only one stream per camera is allowed — a new connection cancels the previous one.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | — | Camera to stream; omit to use smart auto-select |
| `width` | int | config / sensor native | Override frame width in pixels (MindVision only, single-camera path) |
| `height` | int | config / sensor native | Override frame height in pixels (MindVision only, single-camera path) |
| `fps` | float | `stream.fps` from config | Override frames per second (MindVision only, single-camera path) |

`width` and `height` can be set independently. The camera's original resolution is restored when the stream ends.

---

### `POST /rpi/capture`

Capture one frame and return a JPEG. Behaviour depends on whether `camera_id` is supplied and how many cameras are connected:

- **`camera_id` supplied** — captures that specific camera regardless of calibration state.
- **No `camera_id`, multiple cameras** — redirects (302) to [`GET /api/stitch/capture`](#get-apistitchcapture): stitched JPEG if fully calibrated, single-camera JPEG otherwise.
- **No `camera_id`, single camera** — captures camera `0` directly (no redirect).

Returns `429` if a capture is already in progress, `503` if no camera is available, `409` if the camera is in `hardware_trigger` mode.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | — | Camera to capture from; omit to use smart auto-select |
| `width` | int | sensor native | Output width in pixels — must be provided with `height` (single-camera path only) |
| `height` | int | sensor native | Output height in pixels — must be provided with `width` (single-camera path only) |

---

## System configuration

### `GET /api/system/config`

Return effective configuration — TOML base values merged with any runtime overrides. Sensitive keys (`destination_api_key`) are masked.

**Response**

```json
{
  "config": {
    "stream.fps": 15,
    "hw_trigger.destination_url": "https://...",
    "hw_trigger.destination_api_key": "***",
    ...
  },
  "runtime_overrides": ["stream.fps"]
}
```

---

### `PATCH /api/system/config`

Update one or more values at runtime without a server restart. Overrides are persisted to `runtime_config.json`.

**Body** — JSON object of `"section.key": value` pairs:

```json
{"stream.fps": 10, "hw_trigger.save_local": true}
```

| Key | Type |
| --- | ---- |
| `stream.fps` | int |
| `stream.quality` | int |
| `hw_trigger.destination_url` | string |
| `hw_trigger.destination_api_key` | string |
| `hw_trigger.retry_attempts` | int |
| `hw_trigger.timeout_seconds` | int |
| `hw_trigger.save_local` | bool |
| `hw_trigger.local_max_files` | int |
| `hw_trigger.local_max_mb` | int |
| `hw_trigger.raw_destination_url` | string |
| `hw_trigger.send_raw_images` | bool |

---

### `DELETE /api/system/config/<key>`

Remove a runtime override, reverting that key to its `configuration.toml` value. Returns `404` if no override is set for that key.

---

## MindVision — cameras

### `GET /api/cameras`

List all connected cameras.

**Response**

```json
[
  {
    "camera_id": 0,
    "serial_number": "AB1234",
    "model": "MV-SUA134GC",
    "product_name": "MindVision USB3 Color",
    "port_type": "USB3",
    "status": "open"
  }
]
```

---

### `GET /api/cameras/mode`

Return the active mode for a camera.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response** `{"camera_id": 0, "mode": "capture"}`

---

### `POST /api/cameras/mode`

Switch a camera's operating mode.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Body**

```json
{"mode": "capture"}
```

| `mode` value | Behaviour |
| ------------ | --------- |
| `stream` | Continuous grab loop — optimised for MJPEG preview |
| `capture` | Software-triggered grab — one frame per `POST /rpi/capture` |
| `hardware_trigger` | Camera waits for a physical signal on the trigger pin |

---

## MindVision — white balance

### `GET /api/cameras/white-balance`

Return current white balance state and gains.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response** `{"calibrated": true, "auto": false, "r_gain": 112, "g_gain": 100, "b_gain": 138}`

---

### `POST /api/cameras/calibrate-wb`

Run one-shot white balance calibration against the current scene. Point the camera at a neutral grey or white surface first. Gains are persisted to `calibration.json` and applied on every subsequent open.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

---

## MindVision — orientation

### `GET /api/cameras/orientation`

Return current rotation and mirror state.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response** `{"camera_id": 0, "rotation": 0, "h_mirror": false, "v_mirror": false}`

---

### `POST /api/cameras/rotation`

Set SDK rotation. Applied at the ISP level and persisted to device config.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Body**

```json
{"rotation": 2}
```

| Value | Effect |
| ----- | ------ |
| `0` | No rotation |
| `1` | 90° counter-clockwise |
| `2` | 180° |
| `3` | 270° counter-clockwise |

---

### `POST /api/cameras/mirror`

Set SDK mirror. Applied at the ISP level and persisted.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Body**

```json
{"direction": "horizontal", "enable": true}
```

| `direction` | Effect |
| ----------- | ------ |
| `"horizontal"` | Flip left ↔ right |
| `"vertical"` | Flip top ↔ bottom |

---

## MindVision — multi-camera capture

### `POST /api/cameras/capture-all`

Capture one frame from every camera simultaneously and return a ZIP archive. In `hardware_trigger` mode all grab threads block together; in other modes each thread grabs independently.

**Response** — `application/zip` containing `camera_0.jpg`, `camera_1.jpg`, …

---

## MindVision — calibration stream

### `GET /api/cameras/calibration/stream`

MJPEG stream with focus peaking overlay, sharpness trend, exposure status, and optional ChArUco board detection.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |
| `fps` | float | `2` | Stream frame rate (0.5–10) |
| `peak_threshold` | int | `50` | Gradient magnitude cutoff for focus peaking highlights |
| `max_width` | int | `1280` | Downscale to this width before processing |
| `charuco` | int | `0` | Set to `1` to overlay ChArUco board detection status |
| `clip_highlight` | int | `1` | Set to `0` to hide overexposure highlights |
| `show_overlay` | int | `1` | Set to `0` for a clean raw frame with no overlay |

---

### `GET /api/cameras/calibration/score`

Return the current sharpness score and trend for one camera as JSON (single frame, no stream).

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response**

```json
{
  "camera_id": 0,
  "score": 1842.3,
  "session_peak": 2100.0,
  "pct_of_peak": 87.7,
  "trend": "increasing",
  "suggestion": "keep going",
  "history_length": 12,
  "roi": {"x1": 427, "y1": 320, "x2": 853, "y2": 640}
}
```

---

## MindVision — lens placement stream

### `GET /api/cameras/lens/stream`

Lean MJPEG stream for ChArUco board placement during lens calibration. No sharpness computation or focus peaking — just the raw frame with a guide box and ChArUco corner overlay.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |
| `fps` | float | `2` | Stream frame rate (0.5–10) |
| `guide_pct` | int | `40` | Guide box size as % of the shorter frame dimension |
| `max_width` | int | `960` | Downscale width |
| `cx` | float | `0.5` | Guide box centre X as a fraction of frame width |
| `cy` | float | `0.5` | Guide box centre Y as a fraction of frame height |

---

## MindVision — camera settings

### `GET /api/cameras/settings`

Return all tunable SDK settings and their valid ranges for a camera.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response** includes: `ae_enabled`, `exposure_us`, `exposure_min_us`, `exposure_max_us`, `ae_target`, `analog_gain`, `analog_gain_min`, `analog_gain_max`, `r_gain`, `g_gain`, `b_gain`, `r/g/b_gain_min/max`, `sharpness`, `sharpness_min/max`, `gamma`, `gamma_min/max`, `rotation`, `h_mirror`, `v_mirror`.

---

### `POST /api/cameras/settings`

Apply settings to the camera hardware without persisting. Changes are live immediately but lost on camera restart unless followed by `/settings/save`.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Body** — any subset of writable fields:

```json
{
  "ae_enabled": false,
  "exposure_us": 50000,
  "ae_target": 120,
  "analog_gain": 32,
  "r_gain": 112, "g_gain": 100, "b_gain": 138,
  "sharpness": 10,
  "gamma": 100,
  "rotation": 0,
  "h_mirror": false,
  "v_mirror": false
}
```

**Response** `{"camera_id": 0, "applied": ["exposure_us"], "errors": {}}` — `207` if any field failed.

---

### `POST /api/cameras/settings/save`

Apply settings and persist them to the SDK's per-serial config file. Same body as `POST /settings`. Persisted settings survive camera reconnection.

---

### `POST /api/cameras/settings/factory-reset`

Reset camera parameters to SDK factory defaults and persist. Only SDK parameters are affected — lens distortion and stitch calibration files are untouched.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

---

### `GET /api/cameras/settings/stream`

Clean MJPEG stream with no overlay for the settings preview panel.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |
| `fps` | float | `5` | Stream frame rate (0.5–10) |
| `max_width` | int | `1280` | Downscale width |

---

### `GET /api/cameras/settings/snapshot`

Grab a single frame and return it as a JPEG. Works at any exposure time by computing an appropriate grab timeout from the current exposure setting.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |
| `max_width` | int | `1280` | Downscale width |

---

## MindVision — full camera config (read-only)

### `GET /api/cameras/config/full`

Return every SDK parameter grouped by category for one camera. Groups: `device`, `image`, `exposure`, `gain`, `color`, `processing`, `orientation`, `trigger`, `ext_trigger`, `strobe`, `transfer`, `isp_caps`, `stats`. Each param has `key`, `label`, `value`, and `unit`.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

---

### `GET /api/cameras/config/all`

Return full grouped config for every connected camera.

---

### `GET /api/cameras/config/download`

Download full config for every camera as an attached JSON file (`camera_config_<timestamp>.json`).

---

## Decoder (Arduino)

### `POST /api/decoder/start`

Start the serial trigger listener and switch every camera to `hardware_trigger` mode.

**Body** (all optional — falls back to `configuration.toml` values):

```json
{"port": "/dev/ttyACM0", "baud": 115200}
```

Returns `409` if already running. On success returns `{"running": true, "port": "...", "baud": 115200, "camera_mode": "hardware_trigger", "server_health": {...}}`.

---

### `POST /api/decoder/stop`

Stop the serial listener and revert all cameras to `capture` mode. Returns `409` if not running.

---

### `GET /api/decoder/status`

Full listener status including live Arduino state and capture statistics.

**Response**

```json
{
  "running": true,
  "uptime_seconds": 42.3,
  "triggers_received": 12,
  "captures_ok": 12,
  "captures_failed": 0,
  "uploads_ok": 12,
  "uploads_failed": 0,
  "port_present": true,
  "serial_connected": true,
  "speed_cms": 5.2,
  "encoder_count": 118,
  "last_message_at": 1749340189.056,
  "arduino_config": {...},
  "trigger_enabled": true
}
```

---

### `POST /api/decoder/detect`

Probe the configured serial port. If the Arduino is found and the listener is not yet running, automatically sets cameras to `hardware_trigger` mode and starts the listener. No-op if already running or port absent.

---

### `POST /api/decoder/mode/hw-trigger`

Switch to hardware trigger mode: cameras → `HARDWARE_TRIGGER`, sends `set_trigger_enabled=true` to the Arduino. Requires the listener to be running.

---

### `POST /api/decoder/mode/calibration`

Switch to calibration mode: cameras → `CAPTURE`, sends `set_trigger_enabled=false` to the Arduino. Requires the listener to be running.

---

### `POST /api/decoder/trigger/fire`

Send a software trigger over serial — fires one pulse immediately on the Arduino. Returns `409` if listener is not running or not connected.

---

### `POST /api/decoder/reset-count`

Reset the Arduino encoder count and speed to zero.

---

### `GET /api/decoder/config`

Return current Arduino parameters and the physical wheel/encoder configuration.

**Response**

```json
{
  "arduino_config": {"counts_per_cm": 120, "trigger_interval": 60, "pulse_width_ms": 5, "speed_report_interval_ms": 200},
  "physical_config": {"wheel_diameter_mm": 200.0, "encoder_ppr": 600, "capture_interval_mm": 50.0},
  "trigger_enabled": true,
  "arduino_defaults": {...},
  "physical_defaults": {...}
}
```

---

### `PATCH /api/decoder/config`

Update physical wheel/encoder params or raw Arduino params.

Physical keys (`wheel_diameter_mm`, `encoder_ppr`, `capture_interval_mm`) automatically recompute and push `counts_per_cm` and `trigger_interval` to the Arduino. Raw keys (`pulse_width_ms`, `speed_report_interval_ms`) are sent directly. Changes are also persisted to `arduino_config.json`.

**Body** — any subset of settable keys:

```json
{
  "wheel_diameter_mm": 200.0,
  "encoder_ppr": 600,
  "capture_interval_mm": 50.0,
  "pulse_width_ms": 5,
  "speed_report_interval_ms": 200
}
```

---

### `DELETE /api/decoder/config`

Delete `arduino_config.json` so Arduino compile-time defaults take effect on next connect. Also pushes defaults to a running listener.

---

### `GET /api/decoder/diag`

Report trigger mode and frame statistics for every camera. Also fires a software trigger on each camera and reports whether a frame was received — use this to confirm cameras are alive independently of the hardware pin.

---

### `GET /api/decoder/server-health`

Check reachability of the configured `hw_trigger.health_check_url`.

**Response**

```json
{"reachable": true, "status_code": 200}
{"reachable": false, "error": "connection refused"}
{"reachable": null}   // no health_check_url configured
```

---

## Stitch

> These endpoints require MindVision cameras. For calibration workflow and board details see [calibration.md](calibration.md#stitch-calibration-multi-camera).

### `GET /api/stitch/config`

Return current stitch runtime configuration.

**Response** `{"min_corners": 40, "camera_order": null}`

---

### `POST /api/stitch/config`

Update stitch runtime configuration. Changes take effect immediately without restart.

**Body** (all fields optional):

```json
{"min_corners": 40, "camera_order": [1, 0, 2]}
```

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `min_corners` | int | `40` | Minimum ChArUco corners required per camera (minimum 6) |
| `camera_order` | list[int] or null | `null` | Left→right camera order for stitching. `null` = auto (sorted by ID) |

---

### `POST /api/stitch/calibrate`

Capture the specified cameras, detect ChArUco corners, compute a homography for each, and merge into `data/stitch_calibration.json`. Cameras not listed are left untouched in the file.

**Body** (all fields optional):

```json
{"cameras": [0, 1], "board_cols": 20, "board_rows": 14, "square_mm": 10.0, "marker_mm": 8.0, "aruco_dict": "DICT_4X4_250"}
```

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `cameras` | list[int] | all cameras | Camera IDs to calibrate in this pass |
| `board_cols` | int | `20` | Board columns |
| `board_rows` | int | `14` | Board rows |
| `square_mm` | float | `10.0` | Checker square size in mm |
| `marker_mm` | float | `8.0` | ArUco marker size in mm |
| `aruco_dict` | str | `"DICT_4X4_250"` | ArUco dictionary name |

Board spec must match any existing calibration file. `DELETE /api/stitch/calibrate` to start fresh with a different board.

**Response**

```json
{
  "updated": [0, 1],
  "failed": {},
  "grab_errors": {},
  "all_calibrated": [0, 1],
  "canvas": {"width": 2000, "height": 1400}
}
```

Returns `422` if no camera in this pass produced a valid homography.

---

### `GET /api/stitch/calibrate`

Return calibration status. Returns `404` if no calibration file exists.

**Response**

```json
{
  "calibrated": true,
  "cameras_calibrated": [0, 1, 2],
  "cameras_missing": [],
  "ready_to_stitch": true,
  "board": {"cols": 20, "rows": 14, "square_mm": 10.0, "marker_mm": 8.0, "dict": "DICT_4X4_250"},
  "canvas": {"width": 2000, "height": 1400},
  "cameras": {
    "0": {"corners_detected": 156, "inliers": 148, "calibrated_at": "2026-05-25T10:00:00Z"},
    "1": {"corners_detected": 203, "inliers": 198, "calibrated_at": "2026-05-25T10:00:00Z"}
  },
  "color_correction": {"calibrated": true, "calibrated_at": "2026-05-25T10:10:00Z"}
}
```

---

### `DELETE /api/stitch/calibrate`

Delete `data/stitch_calibration.json` and start fresh. Returns `404` if no file exists.

---

### `GET /api/stitch/calibrate-color`

Return per-camera colour correction status.

---

### `POST /api/stitch/calibrate-color`

Capture frames from all cameras pointed at a neutral reference surface and compute per-camera BGR correction multipliers relative to the lowest-ID camera. Requires stitch calibration to already be complete.

**Response**

```json
{
  "calibrated": true,
  "reference_camera": 0,
  "corrections": {"0": {"b": 1.0, "g": 1.0, "r": 1.0}, "1": {"b": 0.97, "g": 1.02, "r": 1.01}},
  "grab_errors": {}
}
```

---

### `DELETE /api/stitch/calibrate-color`

Remove colour correction data without touching the stitch calibration. Returns `404` if none exists.

---

### `GET /api/stitch/capture`

Capture all cameras, apply homographies, blend, and return a single stitched JPEG. Returns `412` with a hint if calibration is missing or incomplete.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `quality` | int | `85` | JPEG quality (1–100) |
| `max_width` | int | `1280` | Cap each input frame width before warping |

---

### `GET /api/stitch/stream`

MJPEG stream of the composite view. Falls back to a single-camera stream if calibration is missing or incomplete.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `fps` | float | `1.0` | Frame rate (0.1–5) |
| `quality` | int | `75` | JPEG quality (1–100) |
| `max_width` | int | `640` | Cap each input frame width before warping (0 = no limit) |
| `camera_id` | int | `0` | Fallback camera when not calibrated |

---

### `GET /api/stitch/detect`

Capture one camera and return a JPEG with detected ChArUco corners drawn on it. Use this before calibrating to verify board visibility and detection quality.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Camera to inspect |
| `quality` | int | `85` | JPEG quality (1–100) |
| `board_cols` | int | `20` | Board columns |
| `board_rows` | int | `14` | Board rows |
| `square_mm` | float | `10.0` | Checker square size in mm |
| `marker_mm` | float | `8.0` | ArUco marker size in mm |
| `aruco_dict` | str | `"DICT_4X4_250"` | ArUco dictionary name |

Overlay label colour:

| Colour | Meaning |
| ------ | ------- |
| Green | ≥ `min_corners` — ready to calibrate |
| Orange | Corners found but too few |
| Red | Board not detected |

---

## Lens calibration

Lens calibration fits camera intrinsics (K, D) from ChArUco frames collected in different positions. Calibrated K/D are used automatically by the stitch pipeline to undistort frames before homography fitting.

### `GET /api/lens`

Return lens calibration status per camera.

**Response**

```json
{
  "0": {
    "calibrated": true,
    "rms": 0.42,
    "frames_used": 18,
    "calibrated_at": "2026-05-25T09:00:00Z",
    "buffered_frames": 0,
    "frames_min": 10,
    "frames_target": 15
  }
}
```

---

### `POST /api/lens/collect`

Grab one frame, detect ChArUco corners, and add to the calibration buffer. Call this from 15–20 different board positions before calling `POST /api/lens/compute`.

Returns `422` if fewer than 6 ChArUco corners are detected — reposition the board and try again.

**Body** (all fields optional):

```json
{
  "camera_id": 0,
  "board_cols": 20,
  "board_rows": 14,
  "square_mm": 10.0,
  "marker_mm": 8.0,
  "aruco_dict": "DICT_4X4_250"
}
```

**Response**

```json
{
  "accepted": true,
  "corners_detected": 42,
  "buffered_frames": 7,
  "hint": "Need 8 more frames — keep collecting"
}
```

---

### `POST /api/lens/compute`

Run lens distortion calibration from the accumulated buffer. Requires at least 10 buffered frames. Stores K and D for the camera, clears the buffer on success.

After this succeeds, re-run `POST /api/stitch/calibrate` so homographies are refitted to undistorted images.

**Body** (all fields optional):

```json
{"camera_id": 0}
```

**Response** `{"camera_id": 0, "rms": 0.42, "frames_used": 18, "hint": "..."}`

Returns `422` if fewer than 10 frames are buffered.

---

### `DELETE /api/lens/last`

Remove the most recently collected frame from the buffer (undo last collect).

**Body** (all fields optional): `{"camera_id": 0}`

---

### `DELETE /api/lens`

Delete stored lens calibration and/or the collection buffer.

**Body** (all fields optional):

```json
{"camera_id": 0, "buffer": true}
```

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `camera_id` | int | (all) | Delete only this camera's data. Omit to clear all. |
| `buffer` | bool | `true` | Also clear the collection buffer |
