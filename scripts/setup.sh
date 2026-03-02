#!/usr/bin/env bash
# Complete one-command setup for visionX-rpi-capture on Raspberry Pi.
# Usage: sudo bash scripts/setup.sh  (or: sudo make setup)
#
# This script:
#   1. Detects OS version and sets the correct boot config path
#   2. Prompts for camera type (Arducam 64MP or standard Pi Camera)
#   3. Arducam only: prompts for CSI port, downloads and installs drivers,
#      patches the boot config with the camera overlay
#   4. Installs system packages (python3-libcamera, python3-kms++)
#   5. Installs uv (if not present) and creates a virtual environment
#   6. Installs Python dependencies
#   7. Copies .env.example → .env
#   8. Installs and enables the rpi-capture systemd service
#   9. Prompts to reboot (the service starts automatically after reboot)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=lib/utils.sh
source "$SCRIPT_DIR/lib/utils.sh"
# shellcheck source=modules/camera.sh
source "$SCRIPT_DIR/modules/camera.sh"

# ── User context ──────────────────────────────────────────────────────────────
# Detect the real (non-root) user who invoked sudo, so that uv, venv, and
# Python dependencies are owned by the correct user rather than root.
if [[ -z "${SUDO_USER:-}" ]]; then
    echo "ERROR: This script must be run via sudo, not from a root shell." >&2
    echo "Usage: sudo bash scripts/setup.sh" >&2
    exit 1
fi

REAL_USER="${SUDO_USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"

# Run a command as the real user with the correct HOME and PATH.
as_user() {
    sudo -u "$REAL_USER" \
        HOME="$REAL_HOME" \
        PATH="$REAL_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        "$@"
}

# ── App setup ─────────────────────────────────────────────────────────────────
_setup_app() {
    log INFO "━━━  App setup  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    cd "$PROJECT_ROOT"

    echo ""
    log INFO "Installing system dependencies..."
    apt update
    apt install -y python3-libcamera python3-kms++ libcap-dev
    log SUCCESS "System dependencies ready."

    echo ""
    log INFO "Checking for uv..."
    if ! as_user command -v uv &>/dev/null; then
        log INFO "Installing uv..."
        as_user bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
        log SUCCESS "uv installed."
    else
        log SUCCESS "uv already installed: $(as_user uv --version)"
    fi

    log INFO "Linking uv to /usr/local/bin/uv..."
    ln -sf "$REAL_HOME/.local/bin/uv" /usr/local/bin/uv
    log SUCCESS "uv available system-wide at /usr/local/bin/uv."

    echo ""
    log INFO "Creating virtual environment with system site-packages..."
    as_user uv venv --system-site-packages
    log SUCCESS "Virtual environment ready."

    echo ""
    log INFO "Installing Python dependencies..."
    as_user uv sync --extra rpi
    log SUCCESS "Python dependencies installed."

    echo ""
    log INFO "Verifying picamera2..."
    if as_user uv run python -c "from picamera2 import Picamera2; print('    picamera2 OK')"; then
        log SUCCESS "Camera library is working."
    else
        log ERROR "picamera2 import failed. Check that the camera is enabled in raspi-config."
        exit 1
    fi

    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        echo ""
        log INFO "Copying .env.example to .env..."
        as_user cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
        log SUCCESS "Created .env — edit to configure: nano $PROJECT_ROOT/.env"
    else
        echo ""
        log INFO ".env already exists, skipping."
    fi

    echo ""
    log INFO "Installing systemd service..."
    local service_name="rpi-capture"
    local service_file="/etc/systemd/system/${service_name}.service"
    tee "$service_file" >/dev/null <<EOF
[Unit]
Description=VisionX RPI Capture API
After=network.target

[Service]
Type=exec
User=${REAL_USER}
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${PROJECT_ROOT}/scripts/start.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$service_name"
    log SUCCESS "Service installed and enabled — will start automatically after reboot."

    log INFO "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ── Reboot prompt ─────────────────────────────────────────────────────────────
_prompt_reboot() {
    echo ""
    log SUCCESS "Setup complete! A reboot is required for all changes to take effect."
    echo ""
    echo "  After rebooting, the rpi-capture service starts automatically."
    echo "  To check status:"
    echo ""
    echo "    sudo systemctl status rpi-capture"
    echo "    journalctl -u rpi-capture -f"
    echo ""
    read -rp "  Reboot now? [y/N] " answer
    case "${answer,,}" in
        y|yes)
            log INFO "Rebooting..."
            reboot
            ;;
        *)
            log WARN "Remember to reboot for changes to take effect."
            ;;
    esac
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    echo ""
    echo "  ┌──────────────────────────────────────┐"
    echo "  │     visionX-rpi-capture  setup       │"
    echo "  └──────────────────────────────────────┘"
    echo ""

    check_root
    detect_os
    setup_camera
    _setup_app
    _prompt_reboot
}

main "$@"
