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

| Method | Path             | Description                                         |
| ------ | ---------------- | --------------------------------------------------- |
| GET    | `/health`        | Health check — `{"status": "ok"}`                   |
| POST   | `/rpi/capture`   | Capture and return an image (JPEG)                  |
| GET    | `/rpi/stream`    | MJPEG live preview stream                           |
| GET    | `/metrics/stats` | Capture performance stats (durations, sizes, ratio) |

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

[stream]
fps = 15                   # Max MJPEG stream frame rate
quality = 60               # JPEG quality 1–95

[capture]
tmp_dir = "/tmp/visionx_captures"

[metrics]
db_path = "/tmp/visionx_metrics.db"

[cleanup]
interval_seconds = 300     # How often stale temp dirs are cleaned up
max_age_seconds  = 300     # Minimum age before removal
```

Camera-specific capture and stream resolutions are defined under `[camera_profiles.*]` — see the file for per-model defaults. These profiles apply to `picamera2` only; MindVision cameras use their native sensor resolution.

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
├── scripts/
│   ├── lib/
│   │   └── utils.sh        # Shared helpers: logging, OS detection, root check
│   ├── modules/
│   │   ├── camera.sh       # Camera selection, Arducam/MindVision driver install
│   │   └── certs.sh        # TLS cert installation (system CA store + Chrome NSS)
│   ├── setup.sh            # Complete setup entry point (run as root)
│   ├── start.sh            # Production server startup (Gunicorn)
│   ├── dev.sh              # Development server (Flask debug mode)
│   └── calibrate.sh        # Live camera preview for lens calibration
├── app.py                  # Flask application and route handlers
├── config.py               # TOML config loader — typed module-level constants
├── camera/
│   ├── __init__.py         # create_camera() factory — returns the right BaseCamera
│   ├── base.py             # BaseCamera ABC: open, close, capture_image, stream_frames
│   ├── picamera.py         # PiCamera — wraps picamera2 for CSI cameras
│   └── mindvision.py       # MindVisionCamera — wraps mvsdk for MindVision USB/GigE cameras
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
