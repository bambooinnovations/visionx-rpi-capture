# Integration Guide

Everything you need to start receiving captures from the RPi.

---

## Quick start

1. **Expose a POST endpoint** on your backend to receive captures (see [What the RPi sends](#what-the-rpi-sends)).
2. **Set `destination_url`** in `configuration.toml` (or via `PATCH /api/system/config`) to point at that endpoint.
3. **Check readiness** via `GET /api/system/ready`, then call `POST /api/system/mode` with `{"mode": "fabric"}` if the system is not yet ready.

---

## What the RPi sends

On every trigger event the RPi POSTs to your configured `destination_url`.

### Stitched capture _(always sent)_

```
POST {destination_url}
Content-Type: multipart/form-data
Authorization: Bearer {destination_api_key}   ← omitted if no key configured

image            — JPEG file, filename: {timestamp_ms}_stitch.jpg
trigger_count    — encoder count at trigger time (string)
trigger_number   — sequential trigger index since decoder start (string)
trigger_source   — "encoder" or "manual"
captured_at      — ISO-8601 UTC timestamp (Pi clock) of when this trigger's frames were captured
```

### Raw per-camera capture _(opt-in)_

Enabled by setting `hw_trigger.send_raw_images = true`. One POST per camera per trigger, sent before the stitched upload.

```
POST {raw_destination_url}
Content-Type: multipart/form-data
Authorization: Bearer {destination_api_key}   ← same key

image            — JPEG file, filename: {timestamp_ms}_{camera_serial}.jpg
trigger_count    — encoder count (string)
trigger_number   — sequential trigger index (string)
trigger_source   — "encoder" or "manual"
camera_id        — index of the camera this frame came from (string, per-camera upload only)
captured_at      — ISO-8601 UTC timestamp (Pi clock), same value for every camera in a trigger group
```

**Your endpoint must return any 2xx.** The RPi calls `raise_for_status()` on the response and retries on 4xx/5xx (3 attempts, 1 s back-off by default). No specific response body is required.

---

## Checking readiness

Before a capture run, confirm every subsystem is ready.

```
GET /api/system/ready
```

Returns a top-level `ready` flag and a per-subsystem breakdown so you can pinpoint exactly what's not ready when the system isn't.

**Example — fully ready:**

```json
{
  "ready": true,
  "subsystems": {
    "cameras": {
      "ready": true,
      "cameras": [
        {
          "id": 0,
          "open": true,
          "mode": "hardware_trigger",
          "serial": "ABC123"
        },
        {
          "id": 1,
          "open": true,
          "mode": "hardware_trigger",
          "serial": "DEF456"
        }
      ]
    },
    "decoder": {
      "ready": true,
      "running": true,
      "serial_connected": true,
      "trigger_enabled": true
    },
    "config": {
      "ready": true,
      "destination_url": "http://192.168.1.50:8888/upload"
    },
    "stitching": {
      "ready": true,
      "calibrated_cameras": [0, 1]
    }
  }
}
```

**Subsystem breakdown:**

| Subsystem   | `ready` when…                                                                                                                                     |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cameras`   | All cameras are open and in `hardware_trigger` mode                                                                                               |
| `decoder`   | Decoder is running, Arduino serial is connected, and triggers are enabled                                                                         |
| `config`    | `destination_url` is set                                                                                                                          |
| `stitching` | All connected cameras have stitch calibration _(multi-camera only; not required for `ready: true` — falls back to single-camera if uncalibrated)_ |

The top-level `ready` is `true` when `cameras`, `decoder`, and `config` are all ready. `stitching` is informational only.

### Switching system mode

```
POST /api/system/mode
{"mode": "fabric" | "regular"}
```

| Mode      | What it does                                                                                        |
| --------- | --------------------------------------------------------------------------------------------------- |
| `fabric`  | Opens cameras, switches to hardware trigger, starts the decoder — full stitched production pipeline |
| `regular` | Stops the decoder, reverts cameras to capture mode — for calibration and software trigger workflows |

The response includes every action attempted, whether it succeeded, and the final subsystem state:

```json
{
  "mode": "fabric",
  "ready": true,
  "actions": [
    {"action": "set_hardware_trigger_mode", "camera_id": 0, "ok": true},
    {"action": "set_hardware_trigger_mode", "camera_id": 1, "ok": true},
    {"action": "start_decoder", "ok": true}
  ],
  "subsystems": { ... }
}
```

If an action fails, `"ok"` is `false` and an `"error"` string explains why. Configuration issues (`destination_url` not set, serial port absent) are not touched — check `subsystems.config` in the response.

---

## Minimal configuration

```toml
[hw_trigger]
destination_url     = "http://<your-host>/upload"
raw_destination_url = "http://<your-host>/upload-raw"   # optional
health_check_url    = "http://<your-host>/health"        # optional
send_raw_images     = false
save_local          = true
```

---

## Reference implementation

The repo includes a minimal FastAPI receiver in `test_server/server.py`. It accepts both upload types, persists every image to `test_server/received/`, and serves a real-time WebSocket monitor page.

**Start it:**

```bash
uv run --group test-server python test_server/server.py
# Listens on port 8888
```

Then open `http://<host>:8888/` in a browser — new captures appear instantly without page refresh.

| Method | Path                 | Description                                                    |
| ------ | -------------------- | -------------------------------------------------------------- |
| `POST` | `/upload`            | Receive stitched captures — set as `destination_url`           |
| `POST` | `/upload-raw`        | Receive raw per-camera captures — set as `raw_destination_url` |
| `GET`  | `/health`            | Health probe — set as `health_check_url`                       |
| `GET`  | `/`                  | Real-time monitor UI                                           |
| `GET`  | `/images/{filename}` | Serve a received image                                         |

---

## RPi endpoint reference

| Method | Path                | Description                                                                            |
| ------ | ------------------- | -------------------------------------------------------------------------------------- |
| `GET`  | `/api/health`       | Liveness check                                                                         |
| `GET`  | `/api/system/ready` | Readiness check with per-subsystem breakdown                                           |
| `POST` | `/api/system/mode`  | Switch to `fabric` (hw trigger + stitch) or `regular` (calibration / software trigger) |

| `POST` | `/api/decoder/trigger/fire` | Fire a one-shot test trigger (same upload pipeline as hardware) |
| `PATCH`| `/api/system/config` | Update `destination_url`, `send_raw_images`, etc. at runtime |
| `GET` | `/api/decoder/status` | Detailed trigger stats and Arduino state |
| `GET` | `/api/metrics/stats` | Capture performance statistics |

See [api.md](api.md) for full parameter and response documentation.
