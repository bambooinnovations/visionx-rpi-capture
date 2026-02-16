#!/usr/bin/env bash
# Live camera preview with continuous autofocus for physical camera calibration.
#
# NOTE: The server must be stopped before running this — libcamera only allows
# one process to access the camera at a time.
#
# Usage: ./scripts/calibrate.sh
# Requires: rpicam-apps (pre-installed on Raspberry Pi OS)
#   If missing: sudo apt install rpicam-apps

set -euo pipefail

echo "Starting calibration preview. Press Ctrl+C to stop."
echo "Make sure the server is not running before using this script."
echo ""

# 2312x1736 uses the full sensor (crop 0,0 / 9248x6944), matching the field of
# view of the actual capture resolution (4624x3472). 1280x720 would crop in.
rpicam-vid \
    --width 2312 \
    --height 1736 \
    --timeout 0
