#!/usr/bin/env bash
# Start the visionx-rpi-capture server in production mode via gunicorn.
# Called by systemd (ExecStart) and can also be run manually for debugging.
#
# Usage: ./scripts/start.sh

set -euo pipefail

# Resolve the project root regardless of where the script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# ── Locate uv ─────────────────────────────────────────────────────────────────
# setup.sh symlinks uv to /usr/local/bin/uv, which is always on PATH.
# If that symlink is absent (manual install or different setup), fall back to
# the standard user install location and add it to PATH explicitly.
if ! command -v uv &>/dev/null; then
    if [ -x "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    else
        echo "ERROR: uv not found."
        echo "  Run 'sudo bash scripts/setup.sh' to complete setup, or install uv manually:"
        echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
fi

echo "Starting visionx-rpi-capture..."
exec env ENV=prod uv run gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 1 \
    --timeout 120 \
    "app:app"
