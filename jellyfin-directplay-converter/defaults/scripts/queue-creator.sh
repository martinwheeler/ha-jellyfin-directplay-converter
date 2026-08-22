#!/usr/bin/env bash
set -euo pipefail

TV_PATH="${TV_PATH:-}"
MOVIES_PATH="${MOVIES_PATH:-}"

BASE_DIR="${BASE_DIR:-/share/jellyfin-media-tools}"
QUEUE_FILE="$BASE_DIR/video_reencode_queue.tsv"
LOCK="$BASE_DIR/queue_scan.lock"
LOG_DIR="$BASE_DIR/logs"
LOG="$LOG_DIR/queue_scan.log"

VIDEO_EXTENSIONS=(mp4 mkv mov avi m4v webm ts mpeg mpg mts m2ts vob)

mkdir -p "$BASE_DIR" "$LOG_DIR"
touch "$QUEUE_FILE" "$LOG"

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"
}

# Prevent overlapping scans and recover automatically from a stale PID lock.
if [[ -f "$LOCK" ]]; then
  old_pid="$(cat "$LOCK" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    log "already running (pid=$old_pid), skipping"
    exit 0
  fi
  log "stale lock found (pid=${old_pid:-unknown}), removing"
  rm -f "$LOCK"
fi

printf '%s\n' "$$" >"$LOCK"
trap 'rm -f "$LOCK"' EXIT

# A converted file can have a new extension, so queue identity is its path stem.
is_stem_in_queue() {
  local stem="$1"
  awk -F'\t' -v stem="$stem" '
    {
      path=$2
      sub(/\.[^\/.]+$/, "", path)
      if (path == stem) { found=1; exit }
    }
    END { exit !found }
  ' "$QUEUE_FILE"
}

queue_add_pending() {
  local file="$1"
  local stem="${file%.*}"

  if is_stem_in_queue "$stem"; then
    return 1
  fi

  printf 'PENDING\t%s\n' "$file" >>"$QUEUE_FILE"
}

process_root() {
  local root="$1"
  local seen=0
  local queued=0
  local skipped=0
  local first=1
  local extension
  local -a extension_args=("(")

  for extension in "${VIDEO_EXTENSIONS[@]}"; do
    if ((first)); then
      extension_args+=(-iname "*.${extension}")
      first=0
    else
      extension_args+=(-o -iname "*.${extension}")
    fi
  done
  extension_args+=(")")

  log "scanning START $root"
  while IFS= read -r -d '' file; do
    ((seen += 1))
    if queue_add_pending "$file"; then
      ((queued += 1))
      log "queued PENDING | $file"
    else
      ((skipped += 1))
    fi
  done < <(
    find "$root" -type f \
      ! -path "*/.trickplay/*" \
      ! -path "$BASE_DIR/*" \
      "${extension_args[@]}" \
      -print0
  )
  log "scanning END $root | seen=$seen queued=$queued skipped=$skipped"
}

SCAN_ROOTS=()
[[ -d "$MOVIES_PATH" ]] && SCAN_ROOTS+=("$MOVIES_PATH")
[[ -d "$TV_PATH" ]] && SCAN_ROOTS+=("$TV_PATH")

if ((${#SCAN_ROOTS[@]} == 0)); then
  log "no valid roots set (TV_PATH/MOVIES_PATH); nothing to do"
  exit 0
fi

log "scan roots set: ${SCAN_ROOTS[*]}"
for root in "${SCAN_ROOTS[@]}"; do
  process_root "$root"
done
log "run complete"
