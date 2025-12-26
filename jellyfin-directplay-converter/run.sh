#!/usr/bin/env bash
set -e

CONFIG_PATH=/data/options.json
SCAN_INTERVAL=$(jq -r '.scan_interval' $CONFIG_PATH)

SCRIPT_DIR="/share/jellyfin-directplay-converter/scripts"
LOG_DIR="/share/jellyfin-directplay-converter/logs"

mkdir -p "$SCRIPT_DIR" "$LOG_DIR"

echo "[INFO] Jellyfin Direct Play Converter started"
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
