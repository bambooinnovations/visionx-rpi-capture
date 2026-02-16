#!/usr/bin/env bash
# Start the visionx-rpi-capture server in production mode via gunicorn.
# Usage: ./scripts/start.sh

set -euo pipefail

# Resolve the project root regardless of where the script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "Starting visionx-rpi-capture (production)..."
exec env ENV=prod uv run gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 1 \
    --timeout 120 \
    "app:app"
