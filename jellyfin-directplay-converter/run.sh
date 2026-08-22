#!/usr/bin/env bash
set -e

CONFIG_PATH=/data/options.json

SCAN_INTERVAL=$(jq -r '.scan_interval' "$CONFIG_PATH")
MOVIES_PATH=$(jq -r '.movies_path' "$CONFIG_PATH")
TV_PATH=$(jq -r '.tv_path' "$CONFIG_PATH")

SCRIPT_DIR="/share/jellyfin-media-tools/scripts"
LOG_DIR="/share/jellyfin-media-tools/logs"
DEFAULT_SCRIPT_DIR="/defaults/scripts"

mkdir -p "$SCRIPT_DIR" "$LOG_DIR"

echo "[INFO] Jellyfin Media Tools started"
echo "[INFO] Scan interval: ${SCAN_INTERVAL} seconds"
echo "[INFO] Movies path: ${MOVIES_PATH}"
echo "[INFO] TV path: ${TV_PATH}"

# 🔑 Copy default scripts on first run only
if [ -z "$(ls -A "$SCRIPT_DIR")" ]; then
  echo "[INFO] No user scripts found, copying defaults"
  cp -a "$DEFAULT_SCRIPT_DIR/." "$SCRIPT_DIR/"
  chmod +x "$SCRIPT_DIR"/*.sh || true
else
  echo "[INFO] User scripts already present, not overwriting"
fi

exec python3 /app.py \
  --scan-interval "$SCAN_INTERVAL" \
  --movies-path "$MOVIES_PATH" \
  --tv-path "$TV_PATH" \
  --script-dir "$SCRIPT_DIR" \
  --log-dir "$LOG_DIR" \
  --queue-path /share/jellyfin-media-tools/video_reencode_queue.tsv
