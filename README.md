# rpi-capture-api

Flask API that captures images from a camera and serves them over HTTP on port **8080**. Supports the Arducam 64MP Hawkeye, standard Pi Cameras (v2, v3, HQ), and MindVision USB/GigE cameras.

## Requirements

| Requirement  | Detail                                                                                              |
| ------------ | --------------------------------------------------------------------------------------------------- |
| **Hardware** | Raspberry Pi 5, 4B, 3B+, 3A+, Zero, Zero 2W, CM3/CM3+/CM4                                         |
| **Camera**   | Arducam 64MP Hawkeye **or** standard Pi Camera v2 / v3 / HQ (MIPI CSI-2) **or** MindVision camera |
| **OS**       | Raspberry Pi OS — Bullseye, Bookworm, or Trixie (64-bit recommended)                               |
| **Internet** | Required during setup (driver downloads, uv installer, TLS certificates)                           |

## Installation

```bash
cd rpi-capture-api
make setup        # or: sudo bash scripts/setup.sh
```

The setup script presents a menu with four options:

| Option | Action |
| ------ | ------ |
| 1 | Check installation status |
| 2 | Install ArduCam (downloads Pivariety drivers, patches boot config, installs app + systemd) |
| 3 | Install MindVision (installs MindVision SDK libraries and installs app + systemd) |
| 4 | Setup TLS certificates (can be run independently at any time) |

After installation the service starts automatically on reboot.

### Verify

```bash
curl http://localhost:8080/health
curl -X POST http://localhost:8080/rpi/capture --output test.jpg
```

## TLS Certificates

The platform frontend is served over HTTPS by a Caddy reverse proxy that uses an internal CA. For the Raspberry Pi's browser (Chromium) and system tools (`curl`, `wget`, Python `requests`) to trust this CA, the setup script installs the root and intermediate certificates.

### What the setup does

The cert module (`scripts/modules/certs.sh`) runs automatically as part of `make setup`:

1. **Downloads** the Caddy internal CA root and intermediate certificates from the [certs repo](https://github.com/bambooinnovations/certs)
2. **System CA store** — copies both certs to `/usr/local/share/ca-certificates/` and runs `update-ca-certificates`, so `curl`, `wget`, Python, and other tools that use the system trust store will accept the platform's TLS certificate
3. **Chrome/Chromium NSS database** — installs `libnss3-tools` (if needed) and adds both certs to the NSS database (`~/.pki/nssdb`) for **every user** on the system (root + all UID >= 1000). If Chrome is running for a user it is restarted so the new certs take effect
4. **Verifies** the root cert against the system CA store

### Manual re-run

To re-install certificates without running the full setup, use option 4 in the setup menu:

```bash
make setup   # then choose option 4
```

Or invoke the cert module directly:

```bash
sudo bash -c '
  source scripts/lib/utils.sh
  source scripts/modules/certs.sh
  setup_certs
'
```

### DNS setup

Each Pi needs host entries pointing to the server running Caddy. Edit `/etc/hosts`:

```
server_ip visionxai.com api.visionxai.com
```

Replace the IP with your actual server address.

### Verifying certificates

After setup (or after a manual re-run), verify the certs are installed correctly:

```bash
# System trust store — should complete without SSL errors
curl https://visionxai.com

# Chromium NSS database — look for "Caddy VisionX Root" with trust flags "C,,"
certutil -d sql:$HOME/.pki/nssdb -L

# If Chromium was open during cert install, restart it to pick up the new certs
pkill -f chromium
```

### Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `curl: (60) SSL certificate problem` | Certs not in system store — re-run setup or the manual command above |
| Chromium shows "Not Secure" / cert warning | NSS certs missing for your user — re-run setup (installs for all users) |
| `certutil: command not found` | `sudo apt install libnss3-tools` |
| Caddy regenerated its CA (e.g. after deleting `caddy/pki/`) | The old certs are invalid — re-run setup to fetch and install the new ones |

## API Endpoints

### Core

| Method | Path             | Description                                         |
| ------ | ---------------- | --------------------------------------------------- |
| GET    | `/health`        | Health check — `{"status": "ok"}`                   |
| POST   | `/rpi/capture`   | Capture and return an image (JPEG)                  |
| GET    | `/rpi/stream`    | MJPEG live preview stream                           |
| GET    | `/metrics/stats` | Capture performance stats (durations, sizes, ratio) |

### Runtime configuration

| Method | Path                    | Description                                         |
| ------ | ----------------------- | --------------------------------------------------- |
| GET    | `/rpi/config`           | Return effective config (toml + runtime overrides)  |
| PATCH  | `/rpi/config`           | Update one or more values at runtime (no restart)   |
| DELETE | `/rpi/config/<key>`     | Remove a runtime override, reverting to toml value  |

PATCH body is a JSON object of `"section.key": value` pairs. Keys that can be updated at runtime:

| Key                              | Type   |
| -------------------------------- | ------ |
| `camera.mv_exposure_us`          | int    |
| `camera.mv_auto_exposure`        | bool   |
| `stream.fps`                     | int    |
| `stream.quality`                 | int    |
| `hw_trigger.destination_url`     | string |
| `hw_trigger.destination_api_key` | string |
| `hw_trigger.retry_attempts`      | int    |
| `hw_trigger.timeout_seconds`     | int    |
| `hw_trigger.save_local`          | bool   |
| `hw_trigger.local_max_files`     | int    |
| `hw_trigger.local_max_mb`        | int    |

Overrides are persisted to `runtime_config.json` (gitignored) and survive restarts.

### MindVision-specific endpoints

Registered only when `camera.type = "mindvision"`. All routes are prefixed `/rpi/mindvision`. Pass `?camera_id=N` to target a specific camera (default `0`).

| Method | Path                                       | Description                                                         |
| ------ | ------------------------------------------ | ------------------------------------------------------------------- |
| GET    | `/rpi/mindvision/cameras`                  | List all connected cameras (index, serial, model, port type)        |
| GET    | `/rpi/mindvision/mode`                     | Get the active mode for a camera                                    |
| POST   | `/rpi/mindvision/mode`                     | Set the active mode (`stream`, `capture`, or `hardware_trigger`)    |
| GET    | `/rpi/mindvision/white-balance`            | Return current white balance gains                                  |
| POST   | `/rpi/mindvision/calibrate-wb`             | Run one-shot white balance calibration and persist gains            |
| POST   | `/rpi/mindvision/capture-all`              | Capture from all cameras simultaneously; returns a ZIP archive      |
| GET    | `/rpi/mindvision/orientation`              | Get current rotation and mirror state                               |
| POST   | `/rpi/mindvision/rotation`                 | Set SDK rotation (0°/90°/180°/270°) and persist                    |
| POST   | `/rpi/mindvision/mirror`                   | Set SDK horizontal or vertical mirror and persist                   |
| GET    | `/rpi/mindvision/calibration/stream`       | MJPEG stream with focus peaking overlay and sharpness score         |
| GET    | `/rpi/mindvision/calibration/score`        | Current sharpness score and trend as JSON (single frame)            |
| GET    | `/rpi/mindvision/edge-detection/stream`    | MJPEG stream with live edge detection overlay (tunable via params)  |
| GET    | `/rpi/mindvision/edge-detection/frame`     | Single JPEG with edge detection applied                             |

#### Camera modes

| Mode               | Behaviour                                                                         |
| ------------------ | --------------------------------------------------------------------------------- |
| `stream`           | Continuous grab loop; optimised for low-latency MJPEG preview                    |
| `capture`          | Triggered grab; each `POST /rpi/capture` pulls one frame                          |
| `hardware_trigger` | Camera waits for a physical signal; each trigger posts the image to `hw_trigger.destination_url` |

Switch modes with:

```bash
curl -X POST http://localhost:8080/rpi/mindvision/mode \
     -H 'Content-Type: application/json' \
     -d '{"mode": "hardware_trigger"}'
```

### `POST /rpi/capture`

Optional query parameters to override capture resolution:

| Parameter | Type | Description             |
| --------- | ---- | ----------------------- |
| `width`   | int  | Output width in pixels  |
| `height`  | int  | Output height in pixels |

Both must be provided together. Defaults to the camera profile's `capture_size` (e.g. `4624×3472` for Arducam 64MP).

Returns `400` if only one dimension is provided, `429` if a capture is already in progress, `503` if no camera is detected.

### `GET /rpi/stream`

Returns a continuous MJPEG stream. Frame rate and JPEG quality are configured in `configuration.toml` under `[stream]`.

## Calibration

### White balance (MindVision)

Point the camera at a neutral white or grey surface under your working light, then run one-push calibration:

```bash
curl -X POST http://localhost:8080/rpi/mindvision/calibrate-wb
# {"r_gain": 112, "g_gain": 100, "b_gain": 138, "calibrated_at": "..."}
```

Gains are stored in `calibration.json` and applied automatically on every subsequent camera open/stream start. To inspect stored gains:

```bash
curl http://localhost:8080/rpi/mindvision/white-balance
```

---

### Focus (MindVision)

MindVision lenses have a manual focus ring. The focus calibration tools let you dial in focus precisely without eyeballing — a live overlay shows which direction to turn and when you've hit peak sharpness.

#### 1. Print the calibration target

Generate a **Siemens star** — a radial spoke wheel that is the industry standard for focus and resolution testing. Run the generator script once, then print the result:

```bash
.venv/bin/python scripts/gen_siemens_star.py
# Saved: siemens_star_letter.png  (2550×3300 px, 300 DPI, letter paper)
```

Print at 100% scale (no "fit to page") on letter paper. Place it flat on a surface, perpendicular to the camera, at your intended working distance.

> The Siemens star has high-spatial-frequency content in every radial direction. When the camera is out of focus the spokes blur together near the centre into a grey disc. As you approach focus the spokes resolve all the way into the small white centre dot.

#### 2. Open the focus stream

Open this URL in a browser (or any MJPEG viewer):

```
http://<rpi-ip>:8080/rpi/mindvision/calibration/stream
```

The stream runs at 2 FPS by default to keep CPU load low on the Pi. You will see:

| Overlay element | What it means |
| --------------- | ------------- |
| **Magenta pixels** | Focus peaking — pixels with high edge contrast. More magenta = sharper in that area. When perfectly focused, magenta highlights fill the spokes all the way to the centre disc. |
| **Yellow ROI box** | The centre-third region used for the sharpness score. Keep the star inside this box. |
| **Score: N** (top-left) | Laplacian variance of the ROI — a dimensionless number; higher is sharper. |
| **↑ / ↓ / ●** (top-left) | Trend arrow based on the rolling history of recent scores. |
| **Sharpness bar** (bottom) | Relative score, normalized to the highest value seen since the stream started. |

#### 3. Adjust focus

Turn the focus ring slowly and watch the trend arrow:

- **↑ keep going** — sharpness is improving; keep turning in the same direction
- **↓ reverse direction** — you just passed peak focus; back off slightly
- **● at or near peak** — you are at or very close to optimal focus

When the trend stabilises at `●` with a full green bar and magenta peaking visible all the way to the centre dot, lock the focus ring.

#### Query parameters

| Parameter       | Default | Description |
| --------------- | ------- | ----------- |
| `camera_id`     | `0`     | Which camera to use |
| `fps`           | `2`     | Stream frame rate (0.5–10) |
| `peak_threshold`| `50`    | Gradient magnitude cutoff for peaking highlights (0–510). Lower = more pixels highlighted; raise if the whole image turns magenta. |
| `max_width`     | `1280`  | Downscale frames to this width before computing and drawing the overlay. Reduces CPU load on high-resolution cameras. |

Example with custom parameters:

```
http://<rpi-ip>:8080/rpi/mindvision/calibration/stream?fps=3&peak_threshold=70&max_width=960
```

#### JSON score endpoint

For scripted calibration or automation, poll the score endpoint instead of streaming:

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
  "roi": {"x1": 427, "y1": 320, "x2": 853, "y2": 640}
}
```

The `score` is the Laplacian variance of the centre ROI — an absolute number that depends on the scene, so use `trend` and `suggestion` for direction guidance rather than comparing scores across different scenes or cameras.

---

### Camera orientation (MindVision)

If a camera is mounted upside-down or sideways, use the rotation and mirror endpoints to correct the output. Settings are applied at the SDK ISP level (no software overhead) and persisted to the per-camera config file so they survive restarts.

#### Get current orientation

```bash
curl http://localhost:8080/rpi/mindvision/orientation
# {"camera_id": 0, "rotation": 0, "h_mirror": false, "v_mirror": false}
```

#### Set rotation

```bash
curl -X POST http://localhost:8080/rpi/mindvision/rotation \
     -H 'Content-Type: application/json' \
     -d '{"rotation": 2}'
```

| `rotation` value | Effect |
| ---------------- | ------ |
| `0` | No rotation (default) |
| `1` | 90° counter-clockwise |
| `2` | 180° |
| `3` | 270° counter-clockwise |

#### Set mirror

```bash
curl -X POST http://localhost:8080/rpi/mindvision/mirror \
     -H 'Content-Type: application/json' \
     -d '{"direction": "horizontal", "enable": true}'
```

| `direction` | Effect |
| ----------- | ------ |
| `"horizontal"` | Flip left ↔ right |
| `"vertical"` | Flip top ↔ bottom |

Both endpoints accept `?camera_id=N` to target a specific camera.

---

### Edge detection (MindVision)

Two endpoints for live parameter tuning and single-frame inspection. All parameters are passed as query strings so you can adjust and reload without reconnecting.

#### Live stream

Open in a browser or any MJPEG viewer:

```
http://<rpi-ip>:8080/rpi/mindvision/edge-detection/stream
```

Current parameter values are overlaid on each frame in green text.

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `camera_id` | `0` | Camera index |
| `method` | `canny` | `canny`, `sobel`, or `laplacian` |
| `fps` | `2` | Stream frame rate (0.5–10) |
| `max_width` | `1280` | Downscale before processing |
| **Canny** | | |
| `low_threshold` | `50` | Lower hysteresis threshold (0–255) |
| `high_threshold` | `150` | Upper hysteresis threshold (0–255) |
| `aperture` | `3` | Sobel aperture inside Canny: `3`, `5`, or `7` |
| `blur_kernel` | `3` | Gaussian pre-blur: `1` (none), `3`, `5`, or `7` |
| **Sobel / Laplacian** | | |
| `ksize` | `3` | Kernel size: `1`, `3`, `5`, or `7` |
| `scale` | `1.0` | Gradient magnitude multiplier |

Example — Canny with tighter thresholds and more blur:

```
http://<rpi-ip>:8080/rpi/mindvision/edge-detection/stream?method=canny&low_threshold=30&high_threshold=80&blur_kernel=5&fps=5
```

#### Single frame

Returns a JPEG with edge detection applied. Accepts the same query params as the stream (minus `fps` and `max_width`):

```bash
curl "http://localhost:8080/rpi/mindvision/edge-detection/frame?method=sobel&ksize=5&scale=2.0" \
     --output edges.jpg
```

---

## Hardware trigger mode (MindVision)

Hardware trigger mode is used when a physical signal — not a software call — fires the camera sensor. An Arduino reads a quadrature encoder (or manual button) and pulses pin D9 directly to the camera's trigger input. At the same moment it sends a JSON line over serial so the Pi knows a frame was captured and can collect it.

```
Encoder/button → Arduino D9 ──► Camera trigger pin  (frame captured by hardware)
                 Arduino TX  ──► Pi serial RX        (JSON notification → collect & upload)
```

### 1. Configure `configuration.toml`

Uncomment and fill in the `[hw_trigger]` section:

```toml
[hw_trigger]
serial_port         = "/dev/ttyACM0"                      # serial port the Arduino is connected to
serial_baud         = 115200                              # must match the Arduino sketch (default 115200)
destination_url     = "https://yoursite.com/api/captures" # where to POST triggered images (leave blank to skip upload)
destination_api_key = ""                                  # sent as Authorization: Bearer <key>; leave blank if not needed
save_local          = true                                # also write a copy to local_save_dir
local_save_dir      = "data/hw_captures"                  # created automatically
local_max_files     = 200                                 # oldest files deleted first (0 = unlimited)
local_max_mb        = 500                                 # oldest files deleted first (0 = unlimited)
```

`destination_url` and `destination_api_key` can also be changed live without a restart:

```bash
curl -X PATCH http://localhost:8080/rpi/config \
     -H 'Content-Type: application/json' \
     -d '{"hw_trigger.destination_url": "https://yoursite.com/api/captures"}'
```

### 2. Start the listener

```bash
curl -X POST http://localhost:8080/rpi/mindvision/serial-trigger/start
```

This switches every connected MindVision camera to `hardware_trigger` mode (SDK waits for the physical pin, no software trigger is issued) and starts reading JSON lines from the serial port. Each trigger event grabs the already-captured frame from the SDK buffer and uploads/saves it.

To override the serial port or baud rate at start time without changing the config file:

```bash
curl -X POST http://localhost:8080/rpi/mindvision/serial-trigger/start \
     -H 'Content-Type: application/json' \
     -d '{"port": "/dev/ttyUSB1", "baud": 115200}'
```

### 3. Check status

```bash
curl http://localhost:8080/rpi/mindvision/serial-trigger/status
```

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

### 4. Stop the listener

```bash
curl -X POST http://localhost:8080/rpi/mindvision/serial-trigger/stop
```

This stops the serial listener and reverts all cameras back to `capture` mode (software trigger).

### Serial trigger endpoints

| Method | Path                                      | Description                                              |
| ------ | ----------------------------------------- | -------------------------------------------------------- |
| POST   | `/rpi/mindvision/serial-trigger/start`    | Start listening; switches cameras to `hardware_trigger`  |
| POST   | `/rpi/mindvision/serial-trigger/stop`     | Stop listening; reverts cameras to `capture` mode        |
| GET    | `/rpi/mindvision/serial-trigger/status`   | Running state, uptime, and capture/upload counters       |
| GET    | `/rpi/mindvision/hw-trigger/server-health`| Check reachability of the configured `health_check_url`  |

### Arduino sketch

The sketch lives at `arduino/decoder_trigger.ino`. Key wiring:

| Pin | Connection |
| --- | ---------- |
| D2  | Encoder channel A |
| D3  | Encoder channel B |
| D4  | Manual trigger button (other end to GND) |
| D9  | Camera trigger output (LOW = trigger active) |

Serial output is 115200 baud. Each trigger event emits one JSON line:

```json
{"type":"trigger","source":"encoder","count":118,"trigger":1}
{"type":"trigger","source":"manual","count":0,"trigger":1}
```

`source` is `"encoder"` for distance-based triggers or `"manual"` for the button. `count` is the cumulative encoder count and `trigger` is the trigger sequence number.

---

## Configuration

`configuration.toml` is gitignored — each deployment keeps its own copy. On a fresh checkout, create yours from the example:

```bash
cp configuration.toml.example configuration.toml
```

All settings live in `configuration.toml`:

```toml
[server]
env = "dev"                # "dev" = coloured console logs, "prod" = JSON

[camera]
type = "picamera2"         # "picamera2" (CSI cameras) or "mindvision" (MindVision USB/GigE)
sharpness = 1.0            # ISP sharpness; 0 = off (picamera2 only)
lock_exposure = false      # Lock AE/AWB after startup for consistent captures (picamera2 only)
# lens_position = 2.0      # Manual focus in dioptres; omit for continuous autofocus (picamera2 only)

# MindVision-specific (only used when type = "mindvision")
# mv_camera_index = 0      # Index into the enumerated MindVision device list
# mv_exposure_us  = 30000  # Exposure time in microseconds (when auto-exposure is off)
# mv_auto_exposure = false # Let the camera's AE algorithm control exposure
# mv_auto_wb = true        # true = continuous auto-WB; false = leave camera hardware defaults
                            # Use POST /rpi/mindvision/calibrate-wb to store fixed gains

[stream]
fps = 15                   # Max MJPEG stream frame rate
quality = 60               # JPEG quality 1–95

[capture]
tmp_dir = "/tmp/visionx_captures"

[metrics]
db_path = "/tmp/visionx_metrics.db"

# Hardware trigger (MindVision only — active when mode = "hardware_trigger")
[hw_trigger]
# destination_url     = "https://yoursite.com/api/captures"
# destination_api_key = ""       # sent as Authorization: Bearer <key>
# retry_attempts      = 3
# timeout_seconds     = 10
# save_local          = true     # also save a local copy
# local_save_dir      = "data/hw_captures"
# local_max_files     = 200      # oldest deleted first (0 = unlimited)
# local_max_mb        = 500      # oldest deleted first (0 = unlimited)

[cleanup]
interval_seconds = 300     # How often stale temp dirs are cleaned up
max_age_seconds  = 300     # Minimum age before removal
```

Camera-specific capture and stream resolutions are defined under `[camera_profiles.*]` — see the file for per-model defaults. These profiles apply to `picamera2` only; MindVision cameras use their native sensor resolution.

The `hw_trigger.destination_url` and `destination_api_key` can be changed without a restart using `PATCH /rpi/config`.

## Make Targets

| Command          | Description                                               |
| ---------------- | --------------------------------------------------------- |
| `make setup`     | Full setup: certs + camera drivers + app + systemd (sudo) |
| `make dev`       | Dev server with auto-reload (stops systemd service first) |
| `make start`     | Start the server (via systemd)                            |
| `make stop`      | Stop the server                                           |
| `make restart`   | Restart the server                                        |
| `make status`    | Check server status                                       |
| `make logs`      | Tail server logs                                          |
| `make calibrate` | Live camera preview for lens calibration                  |
| `make verify`    | Verify picamera2 is working                               |
| `make clean`     | Remove venv, logs, and pid file                           |

## Camera Port (CAM0 vs CAM1)

> Applies to Arducam only. Standard Pi Cameras are detected automatically — no port selection needed.

| Port | Overlay in `config.txt`          | When to use                                     |
| ---- | -------------------------------- | ----------------------------------------------- |
| CAM1 | `dtoverlay=arducam-64mp`         | Single CSI connector (default)                  |
| CAM0 | `dtoverlay=arducam-64mp,cam0`    | Dual-port boards: Raspberry Pi 5, CM4 carriers  |

To change after installation, edit the `dtoverlay` line in `/boot/firmware/config.txt` (Bookworm/Trixie) or `/boot/config.txt` (Bullseye) and reboot.

## Project Structure

```
rpi-capture-api/
├── Makefile                # All commands: setup, start, stop, dev, logs, calibrate
├── configuration.toml.example  # Template — copy to configuration.toml and edit locally
├── configuration.toml      # Runtime config — gitignored, not committed
├── runtime_config.json     # Persisted runtime overrides — gitignored, managed via API
├── scripts/
│   ├── lib/
│   │   └── utils.sh        # Shared helpers: logging, OS detection, root check
│   ├── modules/
│   │   ├── camera.sh       # Camera selection, Arducam/MindVision driver install
│   │   ├── mindvision.sh   # MindVision SDK library installation
│   │   └── certs.sh        # TLS cert installation (system CA store + Chrome NSS)
│   ├── sdk/mindvision/     # Bundled MindVision SDK libraries and headers
│   ├── setup.sh            # Complete setup entry point (run as root)
│   ├── start.sh            # Production server startup (Gunicorn)
│   ├── dev.sh              # Development server (Flask debug mode)
│   └── calibrate.sh        # Live camera preview for lens calibration
├── app.py                  # Flask application and route handlers
├── config.py               # TOML config loader — typed module-level constants
├── runtime_config.py       # Runtime override persistence (GET/PATCH/DELETE /rpi/config)
├── calibration.py          # Calibration data persistence (calibration.json)
├── camera/
│   ├── __init__.py         # create_camera() factory — returns the right BaseCamera
│   ├── base.py             # BaseCamera ABC: open, close, capture_image, stream_frames
│   ├── picamera.py         # PiCamera — wraps picamera2 for CSI cameras
│   ├── mindvision.py       # MindVisionCamera — wraps mvsdk; supports stream/capture/hardware_trigger modes
│   └── mindvision_trigger.py  # SerialTriggerListener — reads Arduino JSON over serial, captures on each trigger
├── blueprints/
│   └── mindvision.py       # MindVision-specific routes (/rpi/mindvision/*); registered only for MindVision cameras
├── mvsdk.py                # MindVision SDK Python bindings
├── log_config.py           # structlog configuration
├── metrics.py              # SQLite-backed capture performance metrics
├── tasks.py                # Background cleanup for stale temp files
├── pyproject.toml
└── requirements.txt
```

## Local Development (without camera)

```bash
uv sync
make dev
```

`picamera2` is only available on Raspberry Pi, and `mvsdk` requires the MindVision SDK to be installed. Without a working camera driver the `/rpi/capture` and `/rpi/stream` endpoints return `503`, but all other endpoints work normally.

## References

- [Arducam 64MP Hawkeye — Documentation](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/64MP-Hawkeye/)
- [Arducam Pivariety V4L2 Driver](https://github.com/ArduCAM/Arducam-Pivariety-V4L2-Driver)
- [Raspberry Pi Camera Documentation](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [MindVision Camera SDK](http://www.mindvision.com.cn/)
