#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------
# Configuration (provided via HA add-on config)
# ------------------------------------------------------------
TV_PATH="${TV_PATH:-}"
MOVIES_PATH="${MOVIES_PATH:-}"

LOG_DIR="/share/jellyfin-media-tools/logs"
LOG="$LOG_DIR/ac3_to_aac.log"
LOCK="/share/jellyfin-media-tools/ac3_to_aac.lock"

mkdir -p "$LOG_DIR"

# ------------------------------------------------------------
# Locking (prevent overlapping runs)
# ------------------------------------------------------------
if [ -f "$LOCK" ]; then
  echo "$(date) already running, skipping" >>"$LOG"
  exit 0
fi

touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# ------------------------------------------------------------
# Build scan order: TV first, then Movies
# ------------------------------------------------------------
SCAN_ROOTS=()
[[ -d "$TV_PATH" ]] && SCAN_ROOTS+=("$TV_PATH")
[[ -d "$MOVIES_PATH" ]] && SCAN_ROOTS+=("$MOVIES_PATH")

if [ "${#SCAN_ROOTS[@]}" -eq 0 ]; then
  echo "$(date) no valid scan paths configured" >>"$LOG"
  exit 0
fi

# ------------------------------------------------------------
# Process a single file
# ------------------------------------------------------------
process_file() {
  local f="$1"

  # Only process known video containers
  case "$f" in
  *.mkv | *.mp4 | *.mov) ;;
  *) return 0 ;;
  esac

  # Skip temp files
  [[ "$f" == *.tmp ]] && return 0

  # Detect first audio codec
  # Detect first audio codec
  local codec
  codec="$(ffprobe -v error -select_streams a:0 \
    -show_entries stream=codec_name -of csv=p=0 "$f" || echo "unknown")"

  echo "$(date) audio codec: ${codec} | file: $f" >>"$LOG"

  # Only convert AC3
  if [ "$codec" != "ac3" ]; then
    echo "$(date) skipping (not ac3)" >>"$LOG"
    return 0
  fi

  local out="${f%.*}.mp4"
  local tmp="${out}.tmp"

  echo "$(date) converting: $f" >>"$LOG"

  if ! ffmpeg -y -nostdin \
    -v warning -stats -stats_period 10 \
    -i "$f" \
    -map 0:v:0 \
    -map 0:a:0 \
    -c:v copy \
    -c:a aac -b:a 192k -ac 2 \
    -movflags +faststart \
    "$tmp"; then
    echo "$(date) ffmpeg failed for: $f" >>"$LOG"
    rm -f "$tmp"
    return 0
  fi

  mv "$tmp" "$out"

  # Remove original only if the filename changed
  if [ "$f" != "$out" ]; then
    rm -f "$f"
  fi

  echo "$(date) done: $out" >>"$LOG"
}

# ------------------------------------------------------------
# Process a root path
#   1) "Slow Horses" folders first
#   2) Everything else
# ------------------------------------------------------------
process_root() {
  local root="$1"
  echo "$(date) scanning root: $root" >>"$LOG"

  # Priority pass: any folder named "Slow Horses"
  find "$root" -type d -name "Slow Horses" -print0 2>/dev/null |
    while IFS= read -r -d '' sh_dir; do
      find "$sh_dir" -type f -print0 2>/dev/null |
        while IFS= read -r -d '' f; do
          process_file "$f"
        done
    done

  # Normal pass: everything else (exclude Slow Horses)
  find "$root" \
    -type d -name "Slow Horses" -prune -o \
    -type f -print0 2>/dev/null |
    while IFS= read -r -d '' f; do
      process_file "$f"
    done
}

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
for root in "${SCAN_ROOTS[@]}"; do
  process_root "$root"
done

echo "$(date) run complete" >>"$LOG"
