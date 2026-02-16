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

### On a Raspberry Pi

Install dependencies (including `picamera2`):

```bash
uv sync --extra rpi
```

Copy and edit the environment file:

```bash
cp .env.example .env
```

Start the server:

```bash
./scripts/start.sh
```

### Local Development (without camera)

```bash
uv sync
python app.py
```

> `picamera2` is only available on Raspberry Pi. Without it, the `/rpi/capture` endpoint will return a 500 error, but all other endpoints work normally.

## Scripts

| Script                 | Description                                                                 |
| ---------------------- | --------------------------------------------------------------------------- |
| `scripts/start.sh`     | Start the server in production mode via Gunicorn                            |
| `scripts/calibrate.sh` | Live camera preview with continuous autofocus for physical lens calibration |

> The server must be stopped before running `calibrate.sh` — `libcamera` only allows one process to access the camera at a time.

## Project Structure

```
.
├── app.py               # Flask application and route handlers
├── imageCapture.py      # picamera2 camera init and image capture logic
├── log_config.py        # structlog configuration
├── metrics.py           # SQLite-backed capture performance metrics
├── tasks.py             # Background cleanup task for stale temp files
├── scripts/
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
