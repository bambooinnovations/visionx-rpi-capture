#!/usr/bin/env bash
# modules/camera.sh — Arducam 64MP Hawkeye driver installation and config
# Requires: lib/utils.sh (log, CONFIG_TXT, CAM_APP_PREFIX)

ARDUCAM_INSTALLER_URL="https://github.com/ArduCAM/Arducam-Pivariety-V4L2-Driver/releases/download/install_script/install_pivariety_pkgs.sh"
ARDUCAM_INSTALLER="/tmp/install_pivariety_pkgs.sh"
DTOVERLAY_LINE="dtoverlay=arducam-64mp"   # default; overridden by _select_cam_port

# ── Download the Arducam installer script ─────────────────────────────────────
_download_installer() {
    if [[ -f "$ARDUCAM_INSTALLER" ]]; then
        log INFO "Arducam installer already present, skipping download."
        return
    fi

    log INFO "Downloading Arducam installer..."
    if ! wget -q -O "$ARDUCAM_INSTALLER" "$ARDUCAM_INSTALLER_URL"; then
        log ERROR "Failed to download installer from: ${ARDUCAM_INSTALLER_URL}"
        exit 1
    fi
    chmod +x "$ARDUCAM_INSTALLER"
    log SUCCESS "Installer downloaded."
}

# ── Install a single pivariety package ────────────────────────────────────────
_install_pkg() {
    local pkg="$1"
    log INFO "Installing package: ${pkg}..."
    if bash "$ARDUCAM_INSTALLER" -p "$pkg"; then
        log SUCCESS "Package ready: ${pkg}"
    else
        log ERROR "Failed to install package: ${pkg}"
        exit 1
    fi
}

# ── Prompt user for CSI port selection ───────────────────────────────────────
_select_cam_port() {
    echo ""
    log INFO "Which CSI port is the camera connected to?"
    echo "        [1]  CAM1 — standard single-port connector  (default)"
    echo "        [0]  CAM0 — secondary port on dual-port boards (Pi 5, CM4)"
    read -rp "  Enter port [1/0, default: 1]: " port_choice

    case "${port_choice}" in
        0)
            DTOVERLAY_LINE="dtoverlay=arducam-64mp,cam0"
            log SUCCESS "Camera port: CAM0"
            ;;
        1|"")
            DTOVERLAY_LINE="dtoverlay=arducam-64mp"
            log SUCCESS "Camera port: CAM1 (default)"
            ;;
        *)
            log WARN "Unrecognised input '${port_choice}' — defaulting to CAM1."
            DTOVERLAY_LINE="dtoverlay=arducam-64mp"
            ;;
    esac
    echo ""
}

# ── Patch config.txt with the camera overlay ─────────────────────────────────
_patch_config() {
    if [[ ! -f "$CONFIG_TXT" ]]; then
        log ERROR "Boot config not found: ${CONFIG_TXT}"
        exit 1
    fi

    # Match any arducam-64mp overlay line regardless of port suffix
    if grep -qF "dtoverlay=arducam-64mp" "$CONFIG_TXT"; then
        log INFO "Overlay already present in ${CONFIG_TXT}, skipping."
        return
    fi

    printf '\n# Arducam 64MP Hawkeye\n%s\n' "$DTOVERLAY_LINE" >> "$CONFIG_TXT"
    log SUCCESS "Added '${DTOVERLAY_LINE}' to ${CONFIG_TXT}"
}

# ── Verify the camera is detected (post-reboot check) ────────────────────────
verify_camera() {
    local app="${CAM_APP_PREFIX}-still"
    if ! command_exists "$app"; then
        log WARN "'${app}' not found — skipping camera detection check."
        return
    fi

    log INFO "Checking camera detection with '${app} --list-cameras'..."
    if "$app" --list-cameras 2>&1 | grep -qi "arducam\|64mp\|hawk"; then
        log SUCCESS "Camera detected successfully."
    else
        log WARN "Camera not detected yet. A reboot may be required."
    fi
}

# ── Public entry point ────────────────────────────────────────────────────────
setup_camera() {
    log INFO "━━━  Camera setup  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    _select_cam_port
    _download_installer
    _install_pkg "libcamera_dev"
    _install_pkg "libcamera_apps"
    _install_pkg "64mp_pi_hawk_eye_kernel_driver"
    _patch_config
    log SUCCESS "Camera setup complete."
    log INFO "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}
