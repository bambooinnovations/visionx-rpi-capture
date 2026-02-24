#!/usr/bin/env bash
# Start the visionx-rpi-capture server in production mode via gunicorn.
#
# Usage:
#   ./scripts/start.sh            # foreground (logs to stdout)
#   ./scripts/start.sh --bg       # background (logs to logs/capture.log)
#   ./scripts/start.sh --stop     # stop the background server

set -euo pipefail

# Ensure uv is on PATH (systemd doesn't load shell profiles).
export PATH="$HOME/.local/bin:/root/.local/bin:$PATH"

# Resolve the project root regardless of where the script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/capture.log"
PID_FILE="$PROJECT_ROOT/.gunicorn.pid"

if [ "${1:-}" = "--stop" ]; then
    if [ -f "$PID_FILE" ]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null && echo "Server stopped." || echo "Server not running."
        rm -f "$PID_FILE"
    else
        echo "No PID file found. Server may not be running."
    fi
    exit 0
fi

if [ "${1:-}" = "--bg" ]; then
    mkdir -p "$LOG_DIR"
    echo "Starting visionx-rpi-capture (background)..."
    echo "  PID file: $PID_FILE"
    echo "  Log file: $LOG_FILE"
    env ENV=prod uv run gunicorn \
        --bind 0.0.0.0:8080 \
        --workers 1 \
        --timeout 120 \
        --pid "$PID_FILE" \
        --access-logfile "$LOG_FILE" \
        --error-logfile "$LOG_FILE" \
        --daemon \
        "app:app"
    echo "Server started (PID: $(cat "$PID_FILE"))."
    exit 0
fi

echo "Starting visionx-rpi-capture (foreground)..."
exec env ENV=prod uv run gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 1 \
    --timeout 120 \
    "app:app"
