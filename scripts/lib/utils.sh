#!/usr/bin/env bash
# lib/utils.sh — shared helpers for visionX-rpi-capture setup scripts

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ── Logging ───────────────────────────────────────────────────────────────────
# Usage: log INFO|SUCCESS|WARN|ERROR "message"
log() {
    local level="$1"; shift
    local msg="$*"
    local ts; ts="$(date '+%H:%M:%S')"
    case "$level" in
        INFO)    echo -e "  ${BLUE}[INFO ]${NC}  ${ts}  ${msg}" ;;
        SUCCESS) echo -e "${GREEN}[  OK  ]${NC}  ${ts}  ${msg}" ;;
        WARN)    echo -e "${YELLOW}[ WARN ]${NC}  ${ts}  ${msg}" ;;
        ERROR)   echo -e "  ${RED}[ERROR]${NC}  ${ts}  ${msg}" >&2 ;;
    esac
}

# ── Root check ────────────────────────────────────────────────────────────────
check_root() {
    if [[ "$EUID" -ne 0 ]]; then
        log ERROR "This script must be run as root. Use: sudo bash install.sh"
        exit 1
    fi
    log SUCCESS "Running as root."
}

# ── OS detection ──────────────────────────────────────────────────────────────
# Sets: OS_CODENAME, CONFIG_TXT, CAM_APP_PREFIX
detect_os() {
    if [[ ! -f /etc/os-release ]]; then
        log ERROR "Cannot detect OS: /etc/os-release not found."
        exit 1
    fi

    # shellcheck source=/dev/null
    source /etc/os-release
    OS_CODENAME="${VERSION_CODENAME:-unknown}"

    case "$OS_CODENAME" in
        bullseye)
            CONFIG_TXT="/boot/config.txt"
            CAM_APP_PREFIX="libcamera"
            ;;
        bookworm|trixie)
            CONFIG_TXT="/boot/firmware/config.txt"
            CAM_APP_PREFIX="rpicam"
            ;;
        *)
            log ERROR "Unsupported OS: '${OS_CODENAME}'. Supported: bullseye, bookworm, trixie."
            exit 1
            ;;
    esac

    export OS_CODENAME CONFIG_TXT CAM_APP_PREFIX
    log SUCCESS "Detected OS: ${OS_CODENAME} | config: ${CONFIG_TXT} | app prefix: ${CAM_APP_PREFIX}"
}

# ── Misc ──────────────────────────────────────────────────────────────────────
command_exists() {
    command -v "$1" &>/dev/null
}
