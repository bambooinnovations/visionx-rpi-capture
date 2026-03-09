#!/usr/bin/env bash
# modules/certs.sh — Install Caddy internal CA certificates
# Requires: lib/utils.sh (log), REAL_USER / REAL_HOME from setup.sh

ROOT_CRT_URL="https://raw.githubusercontent.com/bambooinnovations/certs/refs/heads/main/pki/authorities/local/root.crt"
INTERMEDIATE_CRT_URL="https://raw.githubusercontent.com/bambooinnovations/certs/refs/heads/main/pki/authorities/local/intermediate.crt"

CERT_NAME="caddy-visionx"
SYS_CERT_DIR="/usr/local/share/ca-certificates"
TMP_DIR="/tmp/visionx-certs"

# ── Download certs to temp dir ───────────────────────────────────────────────
_download_certs() {
    mkdir -p "$TMP_DIR"

    log INFO "Downloading root certificate..."
    if ! curl -fsSL -o "$TMP_DIR/root.crt" "$ROOT_CRT_URL"; then
        log ERROR "Failed to download root certificate from: ${ROOT_CRT_URL}"
        exit 1
    fi

    log INFO "Downloading intermediate certificate..."
    if ! curl -fsSL -o "$TMP_DIR/intermediate.crt" "$INTERMEDIATE_CRT_URL"; then
        log ERROR "Failed to download intermediate certificate from: ${INTERMEDIATE_CRT_URL}"
        exit 1
    fi

    log SUCCESS "Certificates downloaded."
}

# ── Install to system CA store ───────────────────────────────────────────────
_install_system_certs() {
    log INFO "Installing to system CA store..."

    cp "$TMP_DIR/root.crt" "${SYS_CERT_DIR}/${CERT_NAME}-root.crt"
    cp "$TMP_DIR/intermediate.crt" "${SYS_CERT_DIR}/${CERT_NAME}-intermediate.crt"
    update-ca-certificates

    log SUCCESS "System CA store updated (curl, wget, Python will trust the cert)."
}

# ── Install certs into a single user's NSS database ──────────────────────────
_install_nss_for_user() {
    local user="$1"
    local home="$2"
    local nssdb="${home}/.pki/nssdb"

    if [[ ! -d "$nssdb" ]]; then
        sudo -u "$user" mkdir -p "$nssdb"
        sudo -u "$user" certutil -d "sql:${nssdb}" -N --empty-password
    fi

    sudo -u "$user" certutil -d "sql:${nssdb}" -A \
        -t "C,," -n "Caddy VisionX Root" -i "$TMP_DIR/root.crt"
    sudo -u "$user" certutil -d "sql:${nssdb}" -A \
        -t "C,," -n "Caddy VisionX Intermediate" -i "$TMP_DIR/intermediate.crt"

    log SUCCESS "  ${user} (${nssdb})"

    # Restart Chrome/Chromium if the user has it running
    if pgrep -u "$user" -f "chrome|chromium" &>/dev/null; then
        sudo -u "$user" pkill -f "chrome|chromium" || true
        log INFO "  Restarted Chrome for ${user} — reopen the browser to continue."
    fi
}

# ── Install to Chrome/Chromium NSS database for all users ────────────────────
_install_chrome_certs() {
    if ! command_exists certutil; then
        log INFO "Installing libnss3-tools for Chrome cert support..."
        apt install -y libnss3-tools
    fi

    log INFO "Installing certificates to Chrome NSS store for all users..."

    local count=0
    while IFS=: read -r user _ uid _ _ home _; do
        # Include root (UID 0) and regular users (UID >= 1000, excluding nobody)
        [[ "$uid" -ne 0 && ( "$uid" -lt 1000 || "$uid" -eq 65534 ) ]] && continue
        # Skip users without a valid home directory
        [[ ! -d "$home" ]] && continue

        _install_nss_for_user "$user" "$home"
        count=$((count + 1))
    done < <(getent passwd)

    log SUCCESS "Chrome certs installed for ${count} user(s)."
}

# ── Verify ───────────────────────────────────────────────────────────────────
_verify_certs() {
    log INFO "Verifying certificate installation..."

    if openssl verify -CApath /etc/ssl/certs "${SYS_CERT_DIR}/${CERT_NAME}-root.crt" &>/dev/null; then
        log SUCCESS "System CA verification passed."
    else
        log WARN "System CA verification failed — TLS connections may not work."
    fi
}

# ── Cleanup ──────────────────────────────────────────────────────────────────
_cleanup_certs() {
    rm -rf "$TMP_DIR"
}

# ── Public entry point ───────────────────────────────────────────────────────
setup_certs() {
    log INFO "━━━  TLS certificate setup  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    _download_certs
    _install_system_certs
    _install_chrome_certs
    _verify_certs
    _cleanup_certs

    log SUCCESS "Certificate setup complete."
    log INFO "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}
