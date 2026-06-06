# TLS Certificate Setup

The platform frontend is served over HTTPS by a Caddy reverse proxy that uses an internal CA. For the Raspberry Pi's browser (Chromium) and system tools (`curl`, `wget`, Python `requests`) to trust this CA, the setup script installs the root and intermediate certificates.

## What the setup does

The cert module (`scripts/modules/certs.sh`) runs automatically as part of `make setup`:

1. **Downloads** the Caddy internal CA root and intermediate certificates from the [certs repo](https://github.com/bambooinnovations/certs)
2. **System CA store** — copies both certs to `/usr/local/share/ca-certificates/` and runs `update-ca-certificates`, so `curl`, `wget`, Python, and other tools that use the system trust store will accept the platform's TLS certificate
3. **Chrome/Chromium NSS database** — installs `libnss3-tools` (if needed) and adds both certs to the NSS database (`~/.pki/nssdb`) for **every user** on the system (root + all UID >= 1000). If Chrome is running for a user it is restarted so the new certs take effect
4. **Verifies** the root cert against the system CA store

## Manual re-run

To re-install certificates without running the full setup, use option 4 in the setup menu:

```bash
make setup   # then choose option 4
```

Or invoke the cert module directly:

```bash
sudo bash -c '
  source scripts/lib/utils.sh
  source scripts/modules/certs.sh
  setup_certs
'
```

## DNS setup

Each Pi needs host entries pointing to the server running Caddy. Edit `/etc/hosts`:

```
server_ip visionxai.com api.visionxai.com
```

Replace the IP with your actual server address.

## Verifying certificates

After setup (or after a manual re-run), verify the certs are installed correctly:

```bash
# System trust store — should complete without SSL errors
curl https://visionxai.com

# Chromium NSS database — look for "Caddy VisionX Root" with trust flags "C,,"
certutil -d sql:$HOME/.pki/nssdb -L

# If Chromium was open during cert install, restart it to pick up the new certs
pkill -f chromium
```

## Troubleshooting

| Symptom                                                     | Fix                                                                        |
| ----------------------------------------------------------- | -------------------------------------------------------------------------- |
| `curl: (60) SSL certificate problem`                        | Certs not in system store — re-run setup or the manual command above       |
| Chromium shows "Not Secure" / cert warning                  | NSS certs missing for your user — re-run setup (installs for all users)    |
| `certutil: command not found`                               | `sudo apt install libnss3-tools`                                           |
| Caddy regenerated its CA (e.g. after deleting `caddy/pki/`) | The old certs are invalid — re-run setup to fetch and install the new ones |
