#!/usr/bin/env bash
set -e

CONFIG_PATH=/data/options.json
SCAN_INTERVAL=$(jq -r '.scan_interval' $CONFIG_PATH)

SCRIPT_DIR="/share/jellyfin-media-tools/scripts"
LOG_DIR="/share/jellyfin-media-tools/logs"
DEFAULT_SCRIPT_DIR="/defaults/scripts"

mkdir -p "$SCRIPT_DIR" "$LOG_DIR"

# 🔑 Copy default scripts on first run only
if [ -z "$(ls -A "$SCRIPT_DIR")" ]; then
  echo "[INFO] No user scripts found, copying defaults"
  cp -a "$DEFAULT_SCRIPT_DIR/." "$SCRIPT_DIR/"
  chmod +x "$SCRIPT_DIR"/*.sh || true
else
  echo "[INFO] User scripts already present, not overwriting"
fi

echo "[INFO] Jellyfin Media Tools started"
echo "[INFO] Scan interval: ${SCAN_INTERVAL} minutes"

while true; do
  echo "[INFO] $(date) running scripts" >>"$LOG_DIR/addon.log"

  for script in "$SCRIPT_DIR"/*.sh; do
    [ -x "$script" ] || continue
    echo "[INFO] Running $script" >>"$LOG_DIR/addon.log"
    "$script" >>"$LOG_DIR/addon.log" 2>&1 || true
  done

  sleep "$((SCAN_INTERVAL * 60))"
done
