#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TAILWIND="$SCRIPT_DIR/tailwindcss"
INPUT="$ROOT_DIR/src/dashboard/static/css/input.css"
OUTPUT="$ROOT_DIR/src/dashboard/static/css/output.css"

if [ ! -f "$TAILWIND" ]; then
  echo "Error: Tailwind CLI not found at $TAILWIND"
  exit 1
fi

if [ "${1:-}" = "--watch" ]; then
  "$TAILWIND" -i "$INPUT" -o "$OUTPUT" --watch
else
  "$TAILWIND" -i "$INPUT" -o "$OUTPUT" --minify
fi
