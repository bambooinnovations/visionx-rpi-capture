#!/usr/bin/env bash
# One-time setup for deploying visionx-rpi-capture on a Raspberry Pi.
# Usage: ./scripts/setup.sh
#
# This script:
#   1. Installs system dependencies (libcamera Python bindings)
#   2. Installs uv (if not already present)
#   3. Creates a venv with access to system site-packages
#   4. Installs Python dependencies (including picamera2)
#   5. Copies .env.example to .env (if .env doesn't exist)
#   6. Installs and enables a systemd service for auto-start on boot

set -euo pipefail

# Resolve the project root regardless of where the script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

SERVICE_NAME="rpi-capture"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "==> Installing system dependencies..."
sudo apt update
sudo apt install -y python3-libcamera python3-kms++

echo ""
echo "==> Checking for uv..."
if ! command -v uv &>/dev/null; then
    echo "    uv not found, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "    uv is already installed: $(uv --version)"
fi

echo ""
echo "==> Creating virtual environment with system site-packages..."
uv venv --system-site-packages

echo ""
echo "==> Installing Python dependencies..."
uv sync --extra rpi

echo ""
echo "==> Verifying picamera2..."
if uv run python -c "from picamera2 import Picamera2; print('    picamera2 OK')"; then
    echo "    Camera library is working."
else
    echo "    WARNING: picamera2 import failed. Check that the camera is enabled in raspi-config."
    exit 1
fi

if [ ! -f .env ]; then
    echo ""
    echo "==> Copying .env.example to .env..."
    cp .env.example .env
    echo "    Edit .env to configure the server (nano .env)"
else
    echo ""
    echo "==> .env already exists, skipping."
fi

echo ""
echo "==> Installing systemd service..."
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=VisionX RPI Capture API
After=network.target

[Service]
Type=exec
User=$(whoami)
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PROJECT_ROOT/scripts/start.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "    Service installed and enabled."
echo ""
echo "Setup complete!"
echo "  Start now:    sudo systemctl start $SERVICE_NAME"
echo "  View logs:    journalctl -u $SERVICE_NAME -f"
echo "  Check status: sudo systemctl status $SERVICE_NAME"
