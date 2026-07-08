# rpi-capture-api

Flask API that captures images from a camera and serves them over HTTP on port **8080**. Supports the Arducam 64MP Hawkeye, standard Pi Cameras (v2, v3, HQ), and MindVision USB/GigE cameras.

## Requirements

| Requirement  | Detail                                                                                            |
| ------------ | ------------------------------------------------------------------------------------------------- |
| **Hardware** | Raspberry Pi 5, 4B, 3B+, 3A+, Zero, Zero 2W, CM3/CM3+/CM4                                         |
| **Camera**   | Arducam 64MP Hawkeye **or** standard Pi Camera v2 / v3 / HQ (MIPI CSI-2) **or** MindVision camera |
| **OS**       | Raspberry Pi OS — Bullseye, Bookworm, or Trixie (64-bit recommended)                              |
| **Internet** | Required during setup (driver downloads, uv installer, TLS certificates)                          |

## Installation

```bash
cd rpi-capture-api
make setup        # or: sudo bash scripts/setup.sh
```

The setup script presents a menu with four options:

| Option | Action                                                                                     |
| ------ | ------------------------------------------------------------------------------------------ |
| 1      | Check installation status                                                                  |
| 2      | Install ArduCam (downloads Pivariety drivers, patches boot config, installs app + systemd) |
| 3      | Install MindVision (installs MindVision SDK libraries and installs app + systemd)          |
| 4      | Setup TLS certificates (can be run independently at any time)                              |

After installation the service starts automatically on reboot.

### Verify

```bash
curl http://localhost:8080/api/health
curl -X POST http://localhost:8080/rpi/capture --output test.jpg
```

## TLS Certificates

The platform frontend is served over HTTPS by a Caddy reverse proxy using an internal CA. The setup script installs the root and intermediate certificates into the system CA store and Chrome/Chromium NSS database automatically.

See **[docs/tls-setup.md](docs/tls-setup.md)** for manual re-run instructions, DNS setup, and troubleshooting.

## API Endpoints

Full parameter and response details: **[docs/api.md](docs/api.md)**

### Core

| Method | Path                 | Description                                         |
| ------ | -------------------- | --------------------------------------------------- |
| GET    | `/api/health`        | Health check — `{"status": "ok"}`                   |
| POST   | `/rpi/capture`       | Capture and return an image (JPEG)                  |
| GET    | `/rpi/stream`        | MJPEG live preview stream                           |
| GET    | `/api/metrics/stats` | Capture performance stats (durations, sizes, ratio) |

### Runtime configuration

| Method | Path                       | Description                                        |
| ------ | -------------------------- | -------------------------------------------------- |
| GET    | `/api/system/config`       | Return effective config (toml + runtime overrides) |
| PATCH  | `/api/system/config`       | Update one or more values at runtime (no restart)  |
| DELETE | `/api/system/config/<key>` | Remove a runtime override, reverting to toml value |

Overrides are persisted to `runtime_config.json` (gitignored) and survive restarts. See [docs/api.md](docs/api.md#system-configuration) for the full list of patchable keys.

### MindVision endpoints

Registered only when `camera.type = "mindvision"`. See [docs/api.md](docs/api.md) for the full reference covering cameras, white balance, orientation, settings, calibration streams, lens calibration, stitch, and decoder endpoints.

## Calibration

- General procedures (focus, white balance, lens distortion, stitch): **[docs/calibration.md](docs/calibration.md)**
- Fabric station step-by-step guide (includes UI screenshots and machine prerequisites): **[docs/frabic-station/calibration.md](docs/frabic-station/calibration.md)**

## Hardware Trigger Mode (MindVision)

An Arduino reads a quadrature encoder and pulses pin D9 directly to the camera's trigger input. The Pi collects the frame over serial and uploads/saves it.

```
Encoder/button → Arduino D9 ──► Camera trigger pin  (frame captured by hardware)
                 Arduino TX  ──► Pi serial RX        (JSON notification → collect & upload)
```

See **[docs/hardware-trigger.md](docs/hardware-trigger.md)** for full setup, configuration, curl examples, and Arduino wiring.

## Configuration

`configuration.toml` is gitignored — each deployment keeps its own copy. On a fresh checkout, create yours from the example:

```bash
cp configuration.toml.example configuration.toml
```

All settings live in `configuration.toml`. Whenever a new key is added here, mirror it into `configuration.toml.example` and this block together — they're kept in sync by hand, not by tooling.

```toml
[server]
env = "dev"                # "dev" = coloured console logs, "prod" = JSON

[station]
type = "fabric"            # "fabric" (production line, hardware-trigger) or "qc" (workstation, software-trigger capture) — MindVision only

[camera]
type = "picamera2"         # "picamera2" (CSI cameras) or "mindvision" (MindVision USB/GigE)
sharpness = 1.0            # ISP sharpness; 0 = off (picamera2 only)
lock_exposure = false      # Lock AE/AWB after startup for consistent captures (picamera2 only)
# lens_position = 2.0      # Manual focus in dioptres; omit for continuous autofocus (picamera2 only)

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

# The keys below are runtime-only (PATCH /api/system/config) — not read from this file.
# trigger_queue_maxsize    = 30    # max triggers per camera queue before dropping with warning
# stitch_memory_budget_mb  = 1024  # RAM cap for pending stitch jobs; excess spills to disk
# raw_memory_budget_mb     = 2048  # RAM cap for pending raw-upload jobs; excess spills to disk
# max_queue_age_s          = 5.0   # drop triggers that have waited longer than this

[cleanup]
interval_seconds = 300     # How often stale temp dirs are cleaned up
max_age_seconds  = 300     # Minimum age before removal
```

Camera-specific capture and stream resolutions are defined under `[camera_profiles.*]` — see the file for per-model defaults. These profiles apply to `picamera2` only; MindVision cameras use their native sensor resolution.

Runtime keys (`destination_url`, `destination_api_key`, queue/memory budgets, etc.) can be changed without a restart via `PATCH /api/system/config`. See [docs/hardware-trigger.md](docs/hardware-trigger.md#memory-and-queue-protection) for memory and queue tuning details.

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

| Port | Overlay in `config.txt`       | When to use                                    |
| ---- | ----------------------------- | ---------------------------------------------- |
| CAM1 | `dtoverlay=arducam-64mp`      | Single CSI connector (default)                 |
| CAM0 | `dtoverlay=arducam-64mp,cam0` | Dual-port boards: Raspberry Pi 5, CM4 carriers |

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
├── runtime_config.py       # Runtime override persistence (GET/PATCH/DELETE /api/system/config)
├── calibration.py          # Calibration data persistence (calibration.json)
├── camera/
│   ├── __init__.py         # create_camera() factory — returns the right BaseCamera
│   ├── base.py             # BaseCamera ABC: open, close, capture_image, stream_frames
│   ├── picamera.py         # PiCamera — wraps picamera2 for CSI cameras
│   ├── mindvision.py       # MindVisionCamera — wraps mvsdk; supports stream/capture/hardware_trigger modes
│   └── mindvision_trigger.py  # SerialTriggerListener — reads Arduino JSON over serial, captures on each trigger
├── blueprints/
│   ├── mindvision.py       # MindVision-specific routes (/api/cameras/*); registered only for MindVision cameras
│   └── stitch.py           # Multi-camera stitch calibration and composite view (/api/stitch/*)
├── docs/
│   ├── api.md              # Full API reference
│   ├── calibration.md      # General calibration procedures (WB, focus, orientation, stitch)
│   ├── hardware-trigger.md # Hardware trigger mode setup guide
│   ├── tls-setup.md        # TLS certificate installation and troubleshooting
│   ├── integration.md      # Integration guide for receiving captures
│   └── frabic-station/     # Fabric station specific guides
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
