# API Reference

All endpoints are served on port **8080**. MindVision-specific endpoints (`/rpi/mindvision/*`) are only registered when `camera.type = "mindvision"` in `configuration.toml`. Stitch endpoints (`/rpi/mindvision/stitch/*`) must be explicitly enabled — see `app.py`.

---

## Core

### `GET /health`

Returns `{"status": "ok"}`. Use for uptime monitoring.

---

### `GET /metrics/stats`

Returns capture performance statistics from the SQLite metrics database.

---

### `GET /rpi/stream`

MJPEG live preview stream. Frame rate and JPEG quality are set in `configuration.toml` under `[stream]`.

For MindVision cameras, the camera must be in `stream` mode. Returns `409` if in a different mode.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Camera to stream |
| `width` | int | config / sensor native | Override frame width in pixels (MindVision only) |
| `height` | int | config / sensor native | Override frame height in pixels (MindVision only) |
| `fps` | float | `stream.fps` from config | Override frames per second for this stream (MindVision only) |

`width` and `height` can be set independently. The camera's original resolution is restored when the stream ends.

---

### `POST /rpi/capture`

Capture one frame and return a JPEG. Returns `429` if a capture is already in progress, `503` if no camera is available. For MindVision cameras, returns `409` if the camera is in `hardware_trigger` mode.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Camera to capture from |
| `width` | int | sensor native | Output width in pixels — must be provided with `height` |
| `height` | int | sensor native | Output height in pixels — must be provided with `width` |

---

## Runtime configuration

### `GET /rpi/config`

Return effective configuration — TOML base values merged with any runtime overrides. Sensitive keys (`destination_api_key`) are masked.

**Response**

```json
{
  "config": {
    "camera.mv_exposure_us": 30000,
    "stream.fps": 15,
    ...
  },
  "runtime_overrides": ["camera.mv_exposure_us"]
}
```

---

### `PATCH /rpi/config`

Update one or more values at runtime without a server restart. Overrides are persisted to `runtime_config.json`.

**Body** — JSON object of `"section.key": value` pairs:

```json
{"camera.mv_exposure_us": 50000, "stream.fps": 10}
```

| Key | Type |
| --- | ---- |
| `camera.mv_exposure_us` | int |
| `camera.mv_auto_exposure` | bool |
| `stream.fps` | int |
| `stream.quality` | int |
| `hw_trigger.destination_url` | string |
| `hw_trigger.destination_api_key` | string |
| `hw_trigger.retry_attempts` | int |
| `hw_trigger.timeout_seconds` | int |
| `hw_trigger.save_local` | bool |
| `hw_trigger.local_max_files` | int |
| `hw_trigger.local_max_mb` | int |

---

### `DELETE /rpi/config/<key>`

Remove a runtime override, reverting that key to its `configuration.toml` value. Returns `404` if no override is set for that key.

---

## MindVision — cameras

### `GET /rpi/mindvision/cameras`

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

### `GET /rpi/mindvision/mode`

Return the active mode for a camera.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response** `{"camera_id": 0, "mode": "capture"}`

---

### `POST /rpi/mindvision/mode`

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

### `GET /rpi/mindvision/white-balance`

Return current white balance state and gains.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response** `{"calibrated": true, "auto": false, "r_gain": 112, "g_gain": 100, "b_gain": 138}`

---

### `POST /rpi/mindvision/calibrate-wb`

Run one-shot white balance calibration against the current scene. Point the camera at a neutral grey or white surface first. Gains are persisted to `calibration.json` and applied on every subsequent open.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

---

## MindVision — orientation

### `GET /rpi/mindvision/orientation`

Return current rotation and mirror state.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response** `{"camera_id": 0, "rotation": 0, "h_mirror": false, "v_mirror": false}`

---

### `POST /rpi/mindvision/rotation`

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

### `POST /rpi/mindvision/mirror`

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

### `POST /rpi/mindvision/capture-all`

Capture one frame from every camera simultaneously and return a ZIP archive. In `hardware_trigger` mode all grab threads block together; in other modes each thread grabs independently.

**Response** — `application/zip` containing `camera_0.jpg`, `camera_1.jpg`, …

---

## MindVision — calibration stream

### `GET /rpi/mindvision/calibration/stream`

MJPEG stream with focus peaking overlay, sharpness trend, exposure status, and optional ChArUco board detection. All info is stacked top-left; the sharpness bar is at the bottom.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |
| `fps` | float | `2` | Stream frame rate (0.5–10) |
| `peak_threshold` | int | `50` | Gradient magnitude cutoff for focus peaking highlights. Lower = more pixels lit; raise if the whole image turns magenta. |
| `max_width` | int | `1280` | Downscale to this width before processing. Reduces CPU load. |
| `charuco` | int | `0` | Set to `1` to overlay ChArUco board detection status (corner count + readiness). |

---

### `GET /rpi/mindvision/calibration/score`

Return the current sharpness score and trend for one camera as JSON (single frame, no stream).

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Target camera |

**Response**

```json
{
  "camera_id": 0,
  "score": 1842.3,
  "trend": "increasing",
  "suggestion": "keep going",
  "history_length": 12,
  "roi": {"x1": 427, "y1": 320, "x2": 853, "y2": 640}
}
```

---

## MindVision — hardware trigger

### `POST /rpi/mindvision/serial-trigger/start`

Start the serial trigger listener. Switches every camera to `hardware_trigger` mode and begins reading JSON trigger events from the Arduino over serial. Each event grabs the buffered frame and uploads/saves it.

**Body** (all optional — falls back to `configuration.toml` values):

```json
{"port": "/dev/ttyACM0", "baud": 115200}
```

---

### `POST /rpi/mindvision/serial-trigger/stop`

Stop the serial listener and revert all cameras to `capture` mode.

---

### `GET /rpi/mindvision/serial-trigger/status`

Return listener state and counters.

**Response**

```json
{
  "running": true,
  "uptime_seconds": 42.3,
  "triggers_received": 12,
  "captures_ok": 12,
  "captures_failed": 0,
  "uploads_ok": 12,
  "uploads_failed": 0
}
```

---

### `GET /rpi/mindvision/hw-trigger/diag`

Diagnostic: report trigger mode and frame stats for every camera. Also fires a software trigger on each camera and reports whether a frame was received — use this to confirm cameras are alive independently of the hardware pin.

---

### `GET /rpi/mindvision/hw-trigger/server-health`

Check reachability of the configured `hw_trigger.health_check_url`.

**Response**

```json
{"reachable": true, "status_code": 200}
{"reachable": false, "error": "connection refused"}
{"reachable": null}   // no health_check_url configured
```

---

## MindVision — stitch

> These endpoints are registered only when the stitch blueprint is enabled in `app.py`. See `blueprints/stitch.py`. For calibration workflow and board details see [calibration.md](calibration.md#stitch-calibration-multi-camera).

### `POST /rpi/mindvision/stitch/calibrate`

Capture the specified cameras, detect ChArUco corners, compute a homography for each, and merge into `data/stitch_calibration.json`. Cameras not listed are left untouched in the file.

**Body**

```json
{"cameras": [0, 1], "px_per_mm": 10.0, "min_corners": 8}
```

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `cameras` | list[int] | all cameras | Camera IDs to calibrate in this pass |
| `px_per_mm` | float | `10.0` | Canvas resolution. Must be consistent across all passes. |
| `min_corners` | int | `8` | Minimum detected ChArUco corners to accept a camera's result |

**Response**

```json
{
  "updated": [0, 1],
  "failed": {},
  "grab_errors": {},
  "all_calibrated": [0, 1],
  "canvas": {"width": 2000, "height": 1400},
  "px_per_mm": 10.0
}
```

Returns `422` if no camera in this pass produced a valid homography.

---

### `GET /rpi/mindvision/stitch/calibrate`

Return calibration status. Returns `404` if no calibration file exists.

**Response**

```json
{
  "calibrated": true,
  "cameras_calibrated": [0, 1, 2],
  "cameras_missing": [],
  "ready_to_stitch": true,
  "px_per_mm": 10.0,
  "canvas": {"width": 2000, "height": 1400},
  "cameras": {
    "0": {"corners_detected": 156, "inliers": 148, "calibrated_at": "2026-05-25T10:00:00Z"},
    "1": {"corners_detected": 203, "inliers": 198, "calibrated_at": "2026-05-25T10:00:00Z"},
    "2": {"corners_detected": 141, "inliers": 135, "calibrated_at": "2026-05-25T10:05:00Z"}
  }
}
```

---

### `DELETE /rpi/mindvision/stitch/calibrate`

Delete `data/stitch_calibration.json` and start fresh. Returns `404` if no file exists.

---

### `GET /rpi/mindvision/stitch/preview`

Capture all cameras, apply homographies, blend, and return a single stitched JPEG. Returns `412` with a hint if calibration is missing or incomplete.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `quality` | int | `85` | JPEG quality (1–100) |

---

### `GET /rpi/mindvision/stitch/stream`

MJPEG stream of the composite view. Falls back to a single-camera stream if calibration is missing or incomplete.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `fps` | float | `1.0` | Frame rate (0.1–5) |
| `quality` | int | `75` | JPEG quality (1–100) |
| `camera_id` | int | `0` | Fallback camera when not calibrated |

---

### `GET /rpi/mindvision/stitch/detect`

Capture one camera and return a JPEG with detected ChArUco corners drawn on it. Use this before calibrating to verify board visibility and detection quality.

| Query param | Type | Default | Description |
| ----------- | ---- | ------- | ----------- |
| `camera_id` | int | `0` | Camera to inspect |
| `quality` | int | `85` | JPEG quality (1–100) |

Overlay label colour:

| Colour | Meaning |
| ------ | ------- |
| Green | ≥ 8 corners — ready to calibrate |
| Orange | Corners found but too few |
| Red | Board not detected |
