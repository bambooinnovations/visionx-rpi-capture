.PHONY: help setup start stop restart status logs dev calibrate verify clean

SERVICE_NAME := rpi-capture
SERVICE_FILE := /etc/systemd/system/$(SERVICE_NAME).service
PROJECT_ROOT := $(shell pwd)
LOG_DIR      := $(PROJECT_ROOT)/logs
LOG_FILE     := $(LOG_DIR)/capture.log
PID_FILE     := $(PROJECT_ROOT)/.gunicorn.pid

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-12s %s\n", $$1, $$2}'

# ---------- Full setup (run once on a fresh Pi) ----------

setup: ## Full setup: camera drivers + app + systemd service (requires sudo)
	sudo bash scripts/setup.sh

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

dev: ## Run dev server in foreground with auto-reload (stops systemd service if running)
	@sudo systemctl stop $(SERVICE_NAME) 2>/dev/null || true
	@bash scripts/dev.sh

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
