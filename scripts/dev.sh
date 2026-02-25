#!/usr/bin/env bash
# Start the development server in the foreground.
# Uses Flask's built-in server with auto-reload on code changes.
# NOT for production — use scripts/start.sh (or make start) for that.
#
# Usage: ./scripts/dev.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# ── Locate uv ─────────────────────────────────────────────────────────────────
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

echo "Starting visionx-rpi-capture (dev mode) — Ctrl+C to stop"
exec env ENV=dev uv run flask --app app run \
    --host 0.0.0.0 \
    --port 8080 \
    --debug \
    --no-reload
