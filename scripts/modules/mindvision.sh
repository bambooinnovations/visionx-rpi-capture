#!/usr/bin/env bash
# modules/mindvision.sh — MindVision MVSDK installation and status checks
# Requires: lib/utils.sh

SDK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../sdk/mindvision" && pwd)"

MVUSB_RULES=(88-mvusb.rules 99-mvusb.rules)
MV_HEADERS=(CameraApi.h CameraDefine.h CameraStatus.h)

# ── Check MindVision installation status ──────────────────────────────────────
check_mindvision() {
    echo ""
    log INFO "━━━  MindVision installation status  ━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    local all_ok=true

    if [[ -f "/lib/libMVSDK.so" ]]; then
        log SUCCESS "Library      /lib/libMVSDK.so"
    else
        log WARN    "Library      /lib/libMVSDK.so  ✗ not installed"
        all_ok=false
    fi

    local headers_ok=true
    for h in "${MV_HEADERS[@]}"; do
        [[ -f "/usr/include/$h" ]] || headers_ok=false
    done
    if $headers_ok; then
        log SUCCESS "Headers      /usr/include/CameraApi.h  CameraDefine.h  CameraStatus.h"
    else
        log WARN    "Headers      one or more header files missing from /usr/include/"
        all_ok=false
    fi

    local rules_ok=true
    for r in "${MVUSB_RULES[@]}"; do
        [[ -f "/etc/udev/rules.d/$r" ]] || rules_ok=false
    done
    if $rules_ok; then
        log SUCCESS "udev rules   /etc/udev/rules.d/88-mvusb.rules  99-mvusb.rules"
    else
        log WARN    "udev rules   one or more rule files missing from /etc/udev/rules.d/"
        all_ok=false
    fi

    echo ""
    if $all_ok; then
        log SUCCESS "MindVision SDK is fully installed."
    else
        log WARN    "MindVision SDK is not fully installed. Run 'Install MindVision' to fix."
    fi
    log INFO "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ── Install MindVision SDK ────────────────────────────────────────────────────
install_mindvision() {
    log INFO "━━━  MindVision SDK installation  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [[ ! -d "$SDK_DIR" ]]; then
        log ERROR "SDK files not found at: ${SDK_DIR}"
        exit 1
    fi

    log INFO "Copying udev rules..."
    for r in "${MVUSB_RULES[@]}"; do
        cp "$SDK_DIR/$r" /etc/udev/rules.d/
    done
    log SUCCESS "udev rules installed."

    log INFO "Copying header files..."
    for h in "${MV_HEADERS[@]}"; do
        cp "$SDK_DIR/include/$h" /usr/include/
    done
    log SUCCESS "Header files installed."

    local arch; arch="$(uname -m)"
    log INFO "Detected architecture: ${arch}"

    local lib_src
    case "$arch" in
        x86_64)  lib_src="$SDK_DIR/lib/x64/libMVSDK.so" ;;
        aarch64) lib_src="$SDK_DIR/lib/arm64/libMVSDK.so" ;;
        i?86)    lib_src="$SDK_DIR/lib/x86/libMVSDK.so" ;;
        armv*hf) lib_src="$SDK_DIR/lib/arm/libMVSDK.so" ;;
        armv*l)  lib_src="$SDK_DIR/lib/arm_softfp/libMVSDK.so" ;;
        *)
            log WARN "Unknown arch '${arch}', falling back to arm64."
            lib_src="$SDK_DIR/lib/arm64/libMVSDK.so"
            ;;
    esac

    if [[ ! -f "$lib_src" ]]; then
        log ERROR "Library not found for arch '${arch}': ${lib_src}"
        exit 1
    fi

    cp "$lib_src" /lib/libMVSDK.so
    log SUCCESS "Library installed: $(basename "$lib_src") → /lib/libMVSDK.so"

    log INFO "Reloading udev rules..."
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
    log SUCCESS "udev rules reloaded."

    echo ""
    log SUCCESS "MindVision SDK installed. Plug in your camera and it should be detected."
    log INFO "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}
