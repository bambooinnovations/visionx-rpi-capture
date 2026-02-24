.PHONY: help setup install env service start stop restart status logs calibrate verify clean

SERVICE_NAME := rpi-capture
SERVICE_FILE := /etc/systemd/system/$(SERVICE_NAME).service
PROJECT_ROOT := $(shell pwd)
LOG_DIR      := $(PROJECT_ROOT)/logs
LOG_FILE     := $(LOG_DIR)/capture.log
PID_FILE     := $(PROJECT_ROOT)/.gunicorn.pid

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-12s %s\n", $$1, $$2}'

# ---------- Full setup (run once on a fresh Pi) ----------

setup: install env service ## Full one-time setup (system deps + venv + .env + systemd)
	@echo ""
	@echo "Setup complete!"
	@echo "  make start   - start the server"
	@echo "  make logs    - tail logs"
	@echo "  make status  - check status"

install: ## Install system deps, uv, venv, and Python packages
	@echo "==> Installing system dependencies..."
	sudo apt update
	sudo apt install -y python3-libcamera python3-kms++
	@echo ""
	@echo "==> Checking for uv..."
	@command -v uv >/dev/null 2>&1 || { echo "    Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	@echo ""
	@echo "==> Creating virtual environment with system site-packages..."
	uv venv --system-site-packages
	@echo ""
	@echo "==> Installing Python dependencies..."
	uv sync --extra rpi
	@$(MAKE) verify

env: ## Copy .env.example to .env (skips if .env exists)
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env from .env.example — edit with: nano .env"; \
	else \
		echo ".env already exists, skipping."; \
	fi

service: ## Install and enable the systemd service
	@echo "==> Installing systemd service..."
	@printf '[Unit]\nDescription=VisionX RPI Capture API\nAfter=network.target\n\n[Service]\nType=exec\nUser=%s\nWorkingDirectory=%s\nExecStart=%s/scripts/start.sh\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n' "$(shell whoami)" "$(PROJECT_ROOT)" "$(PROJECT_ROOT)" | sudo tee $(SERVICE_FILE) >/dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable $(SERVICE_NAME)
	@echo "    Service installed and enabled."

# ---------- Server control ----------

start: ## Start the server (via systemd)
	sudo systemctl start $(SERVICE_NAME)
	@echo "Server started."

stop: ## Stop the server
	sudo systemctl stop $(SERVICE_NAME)
	@echo "Server stopped."

restart: ## Restart the server
	sudo systemctl restart $(SERVICE_NAME)
	@echo "Server restarted."

status: ## Check server status
	sudo systemctl status $(SERVICE_NAME) --no-pager

logs: ## Tail server logs
	journalctl -u $(SERVICE_NAME) -f

# ---------- Utilities ----------

calibrate: stop ## Live camera preview for lens calibration (stops server first)
	@echo "Starting calibration preview. Press Ctrl+C when done."
	@echo ""
	rpicam-vid --width 2312 --height 1736 --timeout 0
	@echo ""
	@echo "Restarting server..."
	@$(MAKE) start

verify: ## Verify picamera2 is working
	@uv run python -c "from picamera2 import Picamera2; print('picamera2 OK')"

clean: ## Remove venv, logs, and pid file
	rm -rf .venv logs .gunicorn.pid
	@echo "Cleaned."
