# RPI Capture API

A lightweight Flask API that captures images from a Raspberry Pi CSI camera (ArduCam 64MP) and serves them over HTTP.

## API Endpoints

| Method | Path             | Description                                                               |
| ------ | ---------------- | ------------------------------------------------------------------------- |
| GET    | `/health`        | Health check, returns `{"status": "ok"}`                                  |
| POST   | `/rpi/capture`   | Capture and return an image (JPEG)                                        |
| GET    | `/metrics/stats` | Aggregate capture performance stats (durations, sizes, compression ratio) |

### `POST /rpi/capture`

Optional query parameters to override the capture resolution:

| Parameter | Type | Description             |
| --------- | ---- | ----------------------- |
| `width`   | int  | Output width in pixels  |
| `height`  | int  | Output height in pixels |

Both `width` and `height` must be provided together. Defaults to `4624x3472` (full-sensor 2×2 binned mode).

Returns `400` if only one dimension is provided, `429` if a capture is already in progress, and `500` on camera error.

## Quick Start

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

### Raspberry Pi Deployment

```bash
git clone https://github.com/bambooinnovations/rpi-capture-api.git
cd rpi-capture-api
make setup
make start
```

Verify:

```bash
curl http://localhost:8080/health
curl -X POST http://localhost:8080/rpi/capture --output test.jpg
```

### Local Development (without camera)

```bash
uv sync
python app.py
```

> `picamera2` is only available on Raspberry Pi. Without it, the `/rpi/capture` endpoint will return a 500 error, but all other endpoints work normally.

## Make Targets

Run `make help` to see all available targets:

| Command          | Description                                                  |
| ---------------- | ------------------------------------------------------------ |
| `make setup`     | Full one-time setup (system deps + venv + .env + systemd)    |
| `make start`     | Start the server (via systemd)                               |
| `make stop`      | Stop the server                                              |
| `make restart`   | Restart the server                                           |
| `make status`    | Check server status                                          |
| `make logs`      | Tail server logs                                             |
| `make calibrate` | Live camera preview for lens calibration (stops server first)|
| `make verify`    | Verify picamera2 is working                                  |
| `make clean`     | Remove venv, logs, and pid file                              |

### Camera Calibration

Use `make calibrate` to open a live video preview for physically positioning and focusing the camera. This automatically stops the server (only one process can access the camera), opens the preview, and restarts the server when you press `Ctrl+C`.

Useful when:

- Setting up a new Pi with the camera for the first time
- Repositioning the camera or changing the mounting distance
- Verifying the field of view and framing before capturing

The preview runs at 2312x1736, matching the full-sensor field of view of the default capture resolution (4624x3472).

> Requires `rpicam-apps` (pre-installed on Raspberry Pi OS). If missing: `sudo apt install rpicam-apps`

## Project Structure

```
.
├── Makefile             # All commands: setup, start, stop, logs, calibrate, etc.
├── app.py               # Flask application and route handlers
├── imageCapture.py      # picamera2 camera init and image capture logic
├── log_config.py        # structlog configuration
├── metrics.py           # SQLite-backed capture performance metrics
├── tasks.py             # Background cleanup task for stale temp files
├── scripts/
│   ├── setup.sh         # One-time Raspberry Pi setup
│   ├── start.sh         # Production server startup (Gunicorn)
│   └── calibrate.sh     # Camera calibration preview
├── pyproject.toml
├── requirements.txt
├── .env.example
└── static/              # Captured images directory
```

## Configuration

Copy `.env.example` to `.env` and adjust as needed.

| Variable                   | Default                    | Description                                                            |
| -------------------------- | -------------------------- | ---------------------------------------------------------------------- |
| `ENV`                      | `dev`                      | Log format: `dev` = coloured console, `prod` = JSON                    |
| `CAPTURE_TMP_DIR`          | `/tmp/visionx_captures`    | Directory for temporary per-request capture files                      |
| `CLEANUP_INTERVAL_SECONDS` | `300`                      | How often (seconds) the background cleanup task runs                   |
| `MAX_AGE_SECONDS`          | `300`                      | Minimum age (seconds) of a temp dir before the cleanup task removes it |
| `METRICS_DB_PATH`          | `/data/visionx_metrics.db` | Path to the SQLite database for capture performance metrics            |
| `CAMERA_SHARPNESS`         | `1.0`                      | ISP sharpness multiplier. Set to `0` for ML defect detection pipelines (disables IPA unsharp mask to avoid artificial edge halos) |
| `LOCK_EXPOSURE`            | `false`                    | Set to `true` to lock AE/AWB after a 2 s settle at startup. Keeps exposure and colour temperature constant across captures for consistent defect scoring |
