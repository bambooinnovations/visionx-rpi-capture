#!/usr/bin/env bash
# setup.sh — Interactive setup menu for visionX-rpi-capture on Raspberry Pi.
# Usage: sudo bash scripts/setup.sh  (or: sudo make setup)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=lib/utils.sh
source "$SCRIPT_DIR/lib/utils.sh"
# shellcheck source=modules/camera.sh
source "$SCRIPT_DIR/modules/camera.sh"
# shellcheck source=modules/certs.sh
source "$SCRIPT_DIR/modules/certs.sh"
# shellcheck source=modules/mindvision.sh
source "$SCRIPT_DIR/modules/mindvision.sh"
# shellcheck source=modules/arduino.sh
source "$SCRIPT_DIR/modules/arduino.sh"

# ── User context ──────────────────────────────────────────────────────────────
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    REAL_USER="${SUDO_USER}"
else
    REAL_USER="$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 { print $1; exit }')"
    if [[ -z "$REAL_USER" ]]; then
        echo "ERROR: No regular user found (UID >= 1000). Create a user first." >&2
        exit 1
    fi
    log INFO "Running from root shell — using '${REAL_USER}' for app ownership."
fi

REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"

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
    log SUCCESS "A reboot is required for all changes to take effect."
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

# ── Menu helpers ──────────────────────────────────────────────────────────────
_print_banner() {
    clear
    echo ""
    echo "  ┌──────────────────────────────────────┐"
    echo "  │     visionX-rpi-capture  setup       │"
    echo "  └──────────────────────────────────────┘"
    echo ""
}

_pause() {
    echo ""
    read -rp "  Press Enter to return to menu..." _
}

# ── Check status submenu ──────────────────────────────────────────────────────
_menu_check_status() {
    while true; do
        _print_banner
        echo "  Check Installation Status"
        echo ""
        echo "    1.  ArduCam 64MP Hawkeye"
        echo "    2.  MindVision"
        echo "    3.  Arduino IDE"
        echo "    0.  Back"
        echo ""
        read -rp "  Enter choice: " choice
        echo ""
        case "$choice" in
            1) check_arducam;    _pause ;;
            2) check_mindvision; _pause ;;
            3) check_arduino;    _pause ;;
            0) return ;;
            *) log WARN "Invalid choice '${choice}'." ; _pause ;;
        esac
    done
}

# ── Main menu ─────────────────────────────────────────────────────────────────
main() {
    check_root
    detect_os

    while true; do
        _print_banner
        echo "    1.  Check installation status"
        echo "    2.  Install ArduCam"
        echo "    3.  Install MindVision"
        echo "    4.  Install Arduino IDE"
        echo "    5.  Setup TLS certificates"
        echo "    0.  Exit"
        echo ""
        read -rp "  Enter choice: " choice
        echo ""
        case "$choice" in
            1)
                _menu_check_status
                ;;
            2)
                install_arducam
                _setup_app
                _prompt_reboot
                ;;
            3)
                install_mindvision
                _prompt_reboot
                ;;
            4)
                install_arduino
                _prompt_reboot
                ;;
            5)
                setup_certs
                _pause
                ;;
            0)
                log INFO "Exiting."
                exit 0
                ;;
            *)
                log WARN "Invalid choice '${choice}'."
                _pause
                ;;
        esac
    done
}

main "$@"
