#!/usr/bin/env bash
# modules/arduino.sh — Arduino IDE legacy (1.8.x) installation for ARM Linux
# Arduino IDE v2 does not support ARM Linux; this installs the last 1.x release.
# Requires: lib/utils.sh

ARDUINO_VERSION="1.8.19"
ARDUINO_INSTALL_DIR="/opt/arduino-${ARDUINO_VERSION}"
ARDUINO_BIN="/usr/local/bin/arduino"

_arduino_tarball_suffix() {
    case "$(uname -m)" in
        aarch64) echo "linuxaarch64" ;;
        armv*)   echo "linuxarm" ;;
        *)
            log ERROR "Unsupported architecture '$(uname -m)' for Arduino IDE ARM build."
            exit 1
            ;;
    esac
}

_arduino_url() {
    echo "https://downloads.arduino.cc/arduino-${ARDUINO_VERSION}-$(_arduino_tarball_suffix).tar.xz"
}

# ── Check Arduino IDE installation status ─────────────────────────────────────
check_arduino() {
    echo ""
    log INFO "━━━  Arduino IDE installation status  ━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    local all_ok=true

    if [[ -d "$ARDUINO_INSTALL_DIR" ]]; then
        log SUCCESS "Install dir  ${ARDUINO_INSTALL_DIR}"
    else
        log WARN    "Install dir  ${ARDUINO_INSTALL_DIR}  ✗ not found"
        all_ok=false
    fi

    if [[ -L "$ARDUINO_BIN" || -f "$ARDUINO_BIN" ]]; then
        log SUCCESS "Launcher     ${ARDUINO_BIN}  → $(readlink -f "$ARDUINO_BIN" 2>/dev/null || echo '?')"
    else
        log WARN    "Launcher     ${ARDUINO_BIN}  ✗ not found"
        all_ok=false
    fi

    if command_exists arduino; then
        local ver; ver="$(arduino --version 2>/dev/null | head -1 || echo 'unknown')"
        log SUCCESS "Version      ${ver}"
    else
        log WARN    "Version      arduino command not in PATH"
        all_ok=false
    fi

    echo ""
    if $all_ok; then
        log SUCCESS "Arduino IDE ${ARDUINO_VERSION} is installed."
    else
        log WARN    "Arduino IDE is not fully installed. Run 'Install Arduino IDE' to fix."
    fi
    log INFO "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ── Install Arduino IDE ───────────────────────────────────────────────────────
install_arduino() {
    if [[ "$EUID" -ne 0 ]]; then
        log ERROR "install_arduino must be run as root. Use: sudo bash scripts/setup.sh"
        exit 1
    fi

    log INFO "━━━  Arduino IDE ${ARDUINO_VERSION} installation (ARM Linux)  ━━━━━━━━━━━━━━━━"

    local arch; arch="$(uname -m)"
    local suffix; suffix="$(_arduino_tarball_suffix)"
    log INFO "Architecture: ${arch} → using ${suffix} build"

    if [[ -d "$ARDUINO_INSTALL_DIR" ]]; then
        log INFO "Arduino IDE already extracted at ${ARDUINO_INSTALL_DIR}, skipping download."
    else
        local tarball="/tmp/arduino-${ARDUINO_VERSION}-${suffix}.tar.xz"

        if [[ -f "$tarball" ]]; then
            log INFO "Found cached tarball at ${tarball}, skipping download."
        else
            log INFO "Downloading Arduino IDE ${ARDUINO_VERSION}..."
            if command_exists wget; then
                wget -q --show-progress -O "$tarball" "$(_arduino_url)"
            elif command_exists curl; then
                curl -L --progress-bar -o "$tarball" "$(_arduino_url)"
            else
                log ERROR "Neither wget nor curl found. Install one and retry."
                exit 1
            fi
            log SUCCESS "Download complete."
        fi

        log INFO "Extracting to ${ARDUINO_INSTALL_DIR}..."
        mkdir -p "$ARDUINO_INSTALL_DIR"
        tar -xf "$tarball" --strip-components=1 -C "$ARDUINO_INSTALL_DIR"
        log SUCCESS "Extraction complete."

        rm -f "$tarball"
    fi

    log INFO "Running Arduino install script..."
    bash "$ARDUINO_INSTALL_DIR/install.sh"
    log SUCCESS "Arduino install script done."

    if [[ ! -f "$ARDUINO_BIN" && ! -L "$ARDUINO_BIN" ]]; then
        ln -sf "$ARDUINO_INSTALL_DIR/arduino" "$ARDUINO_BIN"
        log SUCCESS "Symlink created: ${ARDUINO_BIN} → ${ARDUINO_INSTALL_DIR}/arduino"
    fi

    log INFO "Adding current user to dialout group (needed for serial/USB upload)..."
    usermod -aG dialout "$REAL_USER"
    log SUCCESS "User '${REAL_USER}' added to dialout group (re-login required)."

    echo ""
    log SUCCESS "Arduino IDE ${ARDUINO_VERSION} installed. Run: arduino"
    log INFO "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}
