# Integration Guide

This document describes how the RPi capture server interacts with a receiver (your backend), covering trigger behaviour, image delivery, and the endpoints each side exposes.

---

## Operating modes

Each MindVision camera operates in one of three modes. The active mode determines which trigger paths are available.

| Mode | Set via | Behaviour |
| ---- | ------- | --------- |
| `stream` | `POST /api/cameras/mode` `{"mode":"stream"}` | Continuous grab loop; optimised for MJPEG preview. Capture endpoints return `409`. |
| `capture` | `POST /api/cameras/mode` `{"mode":"capture"}` | One frame per software request. Hardware trigger is ignored. |
| `hardware_trigger` | `POST /api/decoder/start` or `POST /api/cameras/mode` | Camera waits for a physical signal on the trigger pin. Software capture returns `409`. |

Starting the decoder (`POST /api/decoder/start`) switches all cameras to `hardware_trigger` automatically.

---

## Hardware trigger flow

This is the primary production path. The Arduino detects encoder pulses and fires a trigger at a configurable distance interval (`capture_interval_mm`).

```
Arduino encoder pulse
       │
       ▼
Arduino fires trigger → sends JSON event over serial
       │
       ▼
SerialTriggerListener (RPi) receives event
       │
       ├─► All cameras capture simultaneously (thread pool)
       │
       ├─► [if send_raw_images = true]
       │       Upload each raw JPEG → raw_destination_url
       │       filename: {timestamp_ms}_{serial}.jpg
       │
       ├─► Stitch captured frames
       │       • ≥2 cameras + calibration present → full stitch
       │       • Otherwise → first available raw image used as fallback
       │
       └─► Upload stitched JPEG → destination_url
               filename: {timestamp_ms}_stitch.jpg
```

**Side effects per trigger event:**

- If `save_local = true` (default): images are also written to `data/hw_captures/` on the RPi. The directory is capped by `local_max_files` (default 200) and `local_max_mb` (default 500 MB); oldest files are pruned automatically.
- Upload failures are retried up to `retry_attempts` times (default 3) with a linear back-off (1 s, 2 s, …). Each attempt honours `timeout_seconds` (default 10 s).
- Stats (`triggers_received`, `captures_ok`, `uploads_ok`, etc.) are accumulated in memory and visible via `GET /api/decoder/status`.

**Trigger event fields** (included in every upload as form fields — see [What the RPi sends](#what-the-rpi-sends)):

| Field | Description |
| ----- | ----------- |
| `trigger_count` | Running encoder count at the moment of trigger |
| `trigger_number` | Sequential trigger index since decoder start |
| `trigger_source` | Source identifier from the Arduino (`"encoder"` or `"manual"`) |

---

## Software trigger flow

There are two distinct software trigger paths; they behave very differently.

### Via decoder: `POST /api/decoder/trigger/fire`

Sends a one-shot pulse command over serial to the Arduino. The Arduino executes the pulse on its hardware output, which the cameras see as a real hardware trigger signal. The full hardware trigger flow above then runs — including upload and local save.

Requirements: decoder must be running (`POST /api/decoder/start`) and the serial port must be connected. Returns `409` otherwise.

Use this to drive a single test capture without waiting for an encoder event, while keeping all the upload and save behaviour intact.

### Via HTTP: `POST /rpi/capture`

A direct software grab from the camera. The RPi captures a frame and **returns it in the HTTP response body** as a JPEG. Nothing is uploaded to the receiver, nothing is saved locally, and the Arduino is not involved.

Requirements: camera must be in `capture` mode. Returns `409` if the camera is in `hardware_trigger` mode.

Use this for ad-hoc inspection — point a browser or `curl` at it to check framing or exposure without touching the trigger pipeline.

---

## `/rpi/capture` and `/rpi/stream` routing

These two endpoints apply smart dispatch based on how many cameras are connected and whether a `camera_id` is supplied.

### `POST /rpi/capture`

| Condition | Behaviour |
| --------- | --------- |
| `camera_id` supplied | Capture directly from that camera; return JPEG |
| No `camera_id`, multiple cameras | **302 redirect** to `GET /api/stitch/capture` — stitched JPEG if calibrated, single-camera fallback otherwise |
| No `camera_id`, single camera | Capture camera `0` directly; return JPEG |
| Camera in `hardware_trigger` mode | `409` |

Optional query params for the single-camera path: `width`, `height` (must be supplied together).

### `GET /rpi/stream`

| Condition | Behaviour |
| --------- | --------- |
| `camera_id` supplied | Stream that camera directly as MJPEG |
| No `camera_id`, multiple cameras | **Redirect** to `GET /api/stitch/stream` — stitched composite if calibrated, single-camera fallback otherwise |
| No `camera_id`, single camera | Stream camera `0` directly |
| Camera in `hardware_trigger` mode | `409` |

Optional query params for the single-camera path: `width`, `height`, `fps`.

Only one concurrent stream per camera is allowed — a new connection cancels the previous one.

---

## What the RPi sends

On every hardware trigger event the RPi makes one or two HTTP POSTs to the receiver.

### Stitched image upload

```
POST {hw_trigger.destination_url}
Content-Type: multipart/form-data
Authorization: Bearer {hw_trigger.destination_api_key}   ← omitted if no key configured

Form fields:
  image            — JPEG file, filename: {timestamp_ms}_stitch.jpg
  trigger_count    — encoder count at trigger time (string)
  trigger_number   — sequential trigger index (string)
  trigger_source   — "encoder" or "manual"
```

### Raw per-camera upload

Sent only when `hw_trigger.send_raw_images = true` (default: false). One POST per camera per trigger event, before the stitched upload.

```
POST {hw_trigger.raw_destination_url}
Content-Type: multipart/form-data
Authorization: Bearer {hw_trigger.destination_api_key}   ← same key

Form fields:
  image            — JPEG file, filename: {timestamp_ms}_{camera_serial}.jpg
  trigger_count    — encoder count (string)
  trigger_number   — sequential trigger index (string)
  trigger_source   — "encoder" or "manual"
```

### Health probe

When `hw_trigger.health_check_url` is configured, the RPi GETs it on decoder start and on demand via `GET /api/decoder/server-health`. Any 2xx response is treated as healthy.

```
GET {hw_trigger.health_check_url}
```

---

## What the receiver must implement

The minimum contract for receiving stitched captures:

1. **Accept `POST` at the configured URL** — multipart/form-data with an `image` file field plus the three trigger fields.
2. **Return `2xx`** — the RPi calls `raise_for_status()` on the response; any 4xx/5xx counts as a failed upload and triggers a retry.

For raw images, expose a second POST endpoint and set `hw_trigger.raw_destination_url` to point at it.

For the health probe, expose a `GET` endpoint that returns any 2xx and set `hw_trigger.health_check_url` to point at it.

No specific response body schema is required — the RPi discards the body on success.

---

## Reference implementation (test server)

The repo includes a minimal FastAPI receiver in `test_server/server.py`. It accepts both upload types, persists every image to `test_server/received/`, and serves a real-time WebSocket monitor page.

**Start it:**

```bash
uv run --group test-server python test_server/server.py
# Listens on port 8888
```

Then open `http://<host>:8888/` in a browser — new captures appear instantly without page refresh.

**Endpoints:**

| Method | Path | Description |
| ------ | ---- | ----------- |
| `POST` | `/upload` | Receive a stitched capture — set as `destination_url` |
| `POST` | `/upload-raw` | Receive raw per-camera captures — set as `raw_destination_url` |
| `GET` | `/health` | Health probe — set as `health_check_url` |
| `GET` | `/` | Real-time monitor UI (WebSocket-updated) |
| `GET` | `/images/{filename}` | Serve a received image |

**Typical configuration in `configuration.toml`:**

```toml
[hw_trigger]
destination_url     = "http://<test-server-host>:8888/upload"
raw_destination_url = "http://<test-server-host>:8888/upload-raw"
health_check_url    = "http://<test-server-host>:8888/health"
send_raw_images     = false
save_local          = true
```

---

## What the receiver can call on the RPi

The RPi exposes a full REST API on port **8080**. Key endpoints useful from the receiver side:

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/api/health` | Liveness check |
| `GET` | `/api/decoder/status` | Trigger stats, Arduino state, upload counts |
| `POST` | `/api/decoder/trigger/fire` | Fire one software trigger (same pipeline as hardware) |
| `GET` | `/api/decoder/server-health` | RPi-side probe of the receiver's health URL |
| `GET` | `/api/stitch/capture` | On-demand stitched JPEG (camera must not be in hardware_trigger mode) |
| `GET` | `/rpi/stream` | MJPEG preview stream |
| `PATCH` | `/api/system/config` | Update `destination_url`, `send_raw_images`, etc. at runtime |
| `GET` | `/api/metrics/stats` | Capture performance statistics |

See [api.md](api.md) for full parameter and response documentation.
