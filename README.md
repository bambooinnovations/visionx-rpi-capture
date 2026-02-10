# RPI Capture API

A lightweight Flask API that captures images from a Raspberry Pi CSI camera and serves them over HTTP.

## API Endpoints

| Method | Path           | Description                          |
|--------|----------------|--------------------------------------|
| GET    | `/health`      | Health check, returns `{"status": "ok"}` |
| POST   | `/rpi/capture` | Capture and return an image (JPEG/PNG) |

The `/rpi/capture` endpoint returns `429` if a capture is already in progress and `503` if no images are available.

## Quick Start

### Docker (recommended)

```bash
docker compose up --build
```

The API will be available at `http://localhost:8080`.

### Local Development

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Project Structure

```
.
├── app.py               # Flask application
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── static/              # Captured images directory
```

## Configuration

| Variable | Default | Description          |
|----------|---------|----------------------|
| Port     | 8080    | HTTP server port     |
| Workers  | 1       | Gunicorn worker count (must stay at 1 for capture lock) |
