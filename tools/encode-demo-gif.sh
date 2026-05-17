#!/usr/bin/env bash
# tools/encode-demo-gif.sh — encode captured frames into docs/demo.gif.
# Run after tools/capture-demo-gif.js.
#
# Requires: ffmpeg
set -euo pipefail

FRAMES_DIR="${FRAMES_DIR:-/tmp/perflens-gif-frames}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$REPO_ROOT/docs/demo.gif}"
FPS="${FPS:-4}"
WIDTH="${WIDTH:-900}"

if [ ! -d "$FRAMES_DIR" ]; then
    echo "no frames in $FRAMES_DIR — run tools/capture-demo-gif.js first" >&2
    exit 1
fi

cd "$FRAMES_DIR"

ffmpeg -y -framerate "$FPS" -i 'f%03d.png' \
    -vf "scale=${WIDTH}:-1:flags=lanczos,palettegen=stats_mode=diff" \
    -loglevel error palette.png

ffmpeg -y -framerate "$FPS" -i 'f%03d.png' -i palette.png \
    -lavfi "scale=${WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
    -loglevel error "$OUT"

ls -lh "$OUT"
