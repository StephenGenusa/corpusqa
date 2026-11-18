#!/usr/bin/env bash
# Records the README demo: builds the invented library, runs the real app and
# model over it, captures one frame per screen and joins them into a GIF
# that holds each frame for 3.5 seconds.
#
# Usage: demo/make_gif.sh MODEL [CONFIG]
#   MODEL   LiteLLM model the app should use, e.g. lm_studio/gemma-4-e4b-it
#   CONFIG  corpusqa.yaml to copy for the run (default: ./corpusqa.yaml)
# Needs: the model available, ffmpeg. Writes demo/corpusqa-demo.gif.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
model="${1:?usage: demo/make_gif.sh MODEL [CONFIG]}"
config="${2:-$repo/corpusqa.yaml}"
# A short path: it is visible in the frames.
work="/tmp/corpusqa-demo"
library="$work/Concurrency Library"
frames="$work/frames"

rm -rf "$work"
mkdir -p "$frames"
"$repo/.venv/bin/python" "$repo/demo/build_library.py" "$library"
cp "$config" "$work/corpusqa.yaml"

(cd "$repo/gui" && CORPUSQA_DEMO=1 CORPUSQA_DEMO_FOLDER="$library" CORPUSQA_DEMO_MODEL="$model" \
  CORPUSQA_DEMO_OUT="$frames" CORPUSQA_CONFIG="$work/corpusqa.yaml" \
  CORPUSQA_PYTHON="$repo/.venv/bin/python" npm start >"$work/app.log" 2>&1)

cat "$frames/record.json"
count=$(ls "$frames"/*.png | wc -l)
[ "$count" -ge 7 ] || { echo "only $count frames; see $work/app.log" >&2; exit 1; }

# One palette for all frames keeps the colours steady and the file small.
ffmpeg -y -loglevel error -framerate 1/3.5 -pattern_type glob -i "$frames/*.png" \
  -vf "fps=1/3.5,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a" \
  -loop 0 "$repo/demo/corpusqa-demo.gif"
ls -la "$repo/demo/corpusqa-demo.gif"
