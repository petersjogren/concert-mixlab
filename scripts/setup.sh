#!/bin/bash
# Create/refresh the concert-mixlab Python environment in $MIXLAB_HOME/.venv (idempotent, quiet when up to date).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${MIXLAB_HOME:-$HOME/.concert-mixlab}"
VENV="$HOME_DIR/.venv"
REQ="$ROOT/requirements.txt"
STAMP="$VENV/.requirements.sha"

for tool in ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { echo "concert-mixlab: '$tool' not found. Install it first (macOS: brew install ffmpeg)." >&2; exit 1; }
done

want="$(shasum "$REQ" | cut -d' ' -f1)"
if [ -x "$VENV/bin/python" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$want" ]; then exit 0; fi

mkdir -p "$HOME_DIR"
echo "concert-mixlab: setting up Python environment in $VENV …" >&2
if command -v uv >/dev/null; then
  [ -x "$VENV/bin/python" ] || uv venv -q "$VENV"
  uv pip install -q --python "$VENV/bin/python" -r "$REQ"
else
  [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$REQ"
fi
echo "$want" > "$STAMP"
echo "concert-mixlab: environment ready." >&2
