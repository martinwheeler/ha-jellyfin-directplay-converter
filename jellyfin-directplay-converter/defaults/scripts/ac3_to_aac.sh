#!/usr/bin/env bash
set -euo pipefail

LOCK="/tmp/ac3_to_aac.lock"

if [ -f "$LOCK" ]; then
  exit 0
fi

touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

LOG="/share/jellyfin-media-tools/logs/ac3_to_aac.log"

SCAN_PATHS=()

[[ -d "${MOVIES_PATH:-}" ]] && SCAN_PATHS+=("$MOVIES_PATH")
[[ -d "${TV_PATH:-}" ]] && SCAN_PATHS+=("$TV_PATH")

if [ "${#SCAN_PATHS[@]}" -eq 0 ]; then
  echo "$(date) no valid scan paths configured" >>"$LOG"
  exit 0
fi

for ROOT in "${SCAN_PATHS[@]}"; do
  find "$ROOT" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.mov" \) -print0 |
    while IFS= read -r -d '' f; do
      codec="$(ffprobe -v error -select_streams a:0 \
        -show_entries stream=codec_name -of csv=p=0 "$f" || true)"

      [[ "$codec" == "ac3" ]] || continue

      out="${f%.*}.mp4"
      tmp="${out}.tmp"

      echo "$(date) converting: $f" >>"$LOG"

      ffmpeg -y -i "$f" \
        -f mp4 \
        -map 0:v:0 -map 0:a:0 -map 0:s? \
        -c:v copy \
        -c:a aac -b:a 192k -ac 2 \
        -c:s copy \
        -movflags +faststart \
        "$tmp"

      mv "$tmp" "$out"
      rm "$f"

      echo "$(date) done: $out" >>"$LOG"
    done
done
