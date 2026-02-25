# visionX-rpi-capture

A lightweight Flask API that captures images from a Raspberry Pi CSI camera and serves them over HTTP. Supports the Arducam 64MP Hawkeye and standard Pi Cameras (v2, v3, HQ).

---

## Requirements

| Requirement  | Detail                                                               |
| ------------ | -------------------------------------------------------------------- |
| **Hardware** | Raspberry Pi 5, 4B, 3B+, 3A+, Zero, Zero 2W, CM3/CM3+/CM4            |
| **Camera**   | Arducam 64MP Hawkeye **or** standard Pi Camera v2 / v3 / HQ (MIPI CSI-2) |
| **OS**       | Raspberry Pi OS — Bullseye, Bookworm, or Trixie (64-bit recommended)      |
| **Internet** | Required during setup (Arducam: driver download; all: uv installer)        |

---

## Installation

```bash
git clone https://github.com/bambooinnovations/visionx-rpi-capture.git
cd visionx-rpi-capture
make setup        # or: sudo bash scripts/setup.sh
```

The script handles everything in one go:

1. Detects your OS version and sets the correct boot config path
2. **Prompts for camera type** — Arducam 64MP Hawkeye or standard Pi Camera (v2, v3, HQ)
3. *Arducam only:* asks which CSI port (CAM1 default, CAM0 optional), downloads and runs the Pivariety driver installer, installs `libcamera_dev` / `libcamera_apps` / the 64MP kernel driver, and patches the boot config with the camera overlay
4. Installs `python3-libcamera` and `python3-kms++` via apt
5. Installs [uv](https://docs.astral.sh/uv/) if not already present
6. Creates a virtual environment with access to system site-packages
7. Installs Python dependencies (including `picamera2`)
8. Copies `.env.example` to `.env`
9. Installs and enables the `rpi-capture` systemd service
10. Prompts to reboot

After rebooting, the `rpi-capture` service starts automatically.

### Verify

```bash
curl http://localhost:8080/health
curl -X POST http://localhost:8080/rpi/capture --output test.jpg
```

---

## API Endpoints

| Method | Path             | Description                                                               |
| ------ | ---------------- | ------------------------------------------------------------------------- |
| GET    | `/health`        | Health check — returns `{"status": "ok"}`                                 |
| POST   | `/rpi/capture`   | Capture and return an image (JPEG)                                        |
| GET    | `/metrics/stats` | Aggregate capture performance stats (durations, sizes, compression ratio) |

### `POST /rpi/capture`

Optional query parameters to override the capture resolution:

| Parameter | Type | Description             |
| --------- | ---- | ----------------------- |
| `width`   | int  | Output width in pixels  |
| `height`  | int  | Output height in pixels |

Both `width` and `height` must be provided together. Defaults to `4624×3472` (full-sensor 2×2 binned mode).

Returns `400` if only one dimension is provided, `429` if a capture is already in progress, and `500` on camera error.

---

## Configuration

Copy `.env.example` to `.env` and adjust as needed (`make setup` does this automatically).

| Variable                   | Default                    | Description                                                                                                                                              |
| -------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ENV`                      | `dev`                      | Log format: `dev` = coloured console, `prod` = JSON                                                                                                      |
| `CAPTURE_TMP_DIR`          | `/tmp/visionx_captures`    | Directory for temporary per-request capture files                                                                                                        |
| `CLEANUP_INTERVAL_SECONDS` | `300`                      | How often (seconds) the background cleanup task runs                                                                                                     |
| `MAX_AGE_SECONDS`          | `300`                      | Minimum age (seconds) of a temp dir before the cleanup task removes it                                                                                   |
| `METRICS_DB_PATH`          | `/data/visionx_metrics.db` | Path to the SQLite database for capture performance metrics                                                                                              |
| `CAMERA_SHARPNESS`         | `1.0`                      | ISP sharpness multiplier. Set to `0` for ML defect detection pipelines (disables IPA unsharp mask to avoid artificial edge halos)                        |
| `LOCK_EXPOSURE`            | `false`                    | Set to `true` to lock AE/AWB after a 2 s settle at startup. Keeps exposure and colour temperature constant across captures for consistent defect scoring |

---

## Make Targets

Run `make help` to see all available targets:

| Command          | Description                                                   |
| ---------------- | ------------------------------------------------------------- |
| `make setup`     | Full setup: camera drivers + app + systemd service (sudo)     |
| `make start`     | Start the server (via systemd)                                |
| `make stop`      | Stop the server                                               |
| `make restart`   | Restart the server                                            |
| `make status`    | Check server status                                           |
| `make logs`      | Tail server logs                                              |
| `make calibrate` | Live camera preview for lens calibration (stops server first) |
| `make verify`    | Verify picamera2 is working                                   |
| `make clean`     | Remove venv, logs, and pid file                               |

---

## Scripts Reference

### `scripts/setup.sh`

```bash
make setup               # recommended
sudo bash scripts/setup.sh   # alternative
```

Complete one-command setup. Must be run as root. Installs camera drivers, the Python app environment, and the systemd service — then prompts to reboot. The service starts automatically after reboot.

- Detects OS codename (Bullseye / Bookworm / Trixie) and selects the correct boot config path
- Prompts for camera type: **Arducam 64MP Hawkeye** or **standard Pi Camera** (v2, v3, HQ)
- *Arducam only:* prompts for CSI port (CAM1 / CAM0), downloads and runs the Pivariety V4L2 driver installer, installs `libcamera_dev`, `libcamera_apps`, `64mp_pi_hawk_eye_kernel_driver`, and appends `dtoverlay=arducam-64mp` (or `dtoverlay=arducam-64mp,cam0`) to the boot config
- *Standard Pi Camera:* no extra drivers — libcamera support is installed via apt
- Installs `python3-libcamera` and `python3-kms++` system packages
- Installs [uv](https://docs.astral.sh/uv/) and creates `.venv` with `--system-site-packages`
- Runs uv, venv, and Python dependency installs as the invoking user (not root)
- Copies `.env.example` → `.env` if `.env` does not exist
- Writes and enables `/etc/systemd/system/rpi-capture.service`
- Safe to re-run — skips steps already completed

### `scripts/start.sh`

```bash
./scripts/start.sh   # foreground (logs to stdout)
```

Starts the Flask app via Gunicorn. Used directly by the systemd service. Logs stream to stdout, which systemd captures via `journalctl -u rpi-capture`.

### `scripts/calibrate.sh`

```bash
./scripts/calibrate.sh   # or: make calibrate
```

Opens a live camera preview using `rpicam-vid` for physically positioning and focusing the lens. The server must be stopped before running this — libcamera only allows one process to access the camera at a time (`make calibrate` handles stop/restart automatically).

- Preview runs at 2312×1736, which matches the full-sensor field of view of the default capture resolution (4624×3472)
- Press `Ctrl+C` to stop the preview
- Requires `rpicam-apps` (pre-installed on Raspberry Pi OS; if missing: `sudo apt install rpicam-apps`)

Useful when:

- Setting up a new Pi with the camera for the first time
- Repositioning the camera or changing the mounting distance
- Verifying field of view and framing before capturing

---

## Camera Port (CAM0 vs CAM1)

Most Raspberry Pi boards have a single CSI connector labelled **CAM1**. The setup script defaults to this port.

> **Applies to Arducam only.** Standard Pi Cameras (v2, v3, HQ) are detected automatically by libcamera — no port selection or overlay is required.

| Port | Overlay written to `config.txt` | When to use                                          |
| ---- | ------------------------------- | ---------------------------------------------------- |
| CAM1 | `dtoverlay=arducam-64mp`        | Standard setup — single CSI connector (default)      |
| CAM0 | `dtoverlay=arducam-64mp,cam0`   | Dual-port boards: Raspberry Pi 5, CM4 carrier boards |

> To change the port after installation, edit the `dtoverlay` line in `/boot/firmware/config.txt` (Bookworm/Trixie) or `/boot/config.txt` (Bullseye) and reboot.

---

## Project Structure

```
visionx-rpi-capture/
├── Makefile                # All commands: setup, start, stop, logs, calibrate, etc.
├── scripts/
│   ├── lib/
│   │   └── utils.sh        # Shared helpers: coloured logging, OS detection, root check
│   ├── modules/
│   │   └── camera.sh       # Camera type selection; Arducam driver install + config patching
│   ├── setup.sh            # Complete setup: camera drivers + app + systemd (run as root)
│   ├── start.sh            # Production server startup (Gunicorn)
│   └── calibrate.sh        # Live camera preview for lens calibration
├── app.py                  # Flask application and route handlers
├── imageCapture.py         # picamera2 camera init and image capture logic
├── log_config.py           # structlog configuration
├── metrics.py              # SQLite-backed capture performance metrics
├── tasks.py                # Background cleanup task for stale temp files
├── pyproject.toml
├── requirements.txt
├── .env.example
└── static/                 # Captured images directory
```

---

## Local Development (without camera)

```bash
uv sync
python app.py
```

> `picamera2` is only available on Raspberry Pi. Without it, the `/rpi/capture` endpoint returns `500`, but all other endpoints work normally.

---

## References

- [Arducam 64MP Hawkeye — Official Documentation](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/64MP-Hawkeye/)
- [Arducam Pivariety V4L2 Driver](https://github.com/ArduCAM/Arducam-Pivariety-V4L2-Driver)
- [Raspberry Pi libcamera Documentation](https://www.raspberrypi.com/documentation/computers/camera_software.html)
