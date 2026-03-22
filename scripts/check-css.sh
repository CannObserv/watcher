#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TAILWIND="$SCRIPT_DIR/tailwindcss"
INPUT="$ROOT_DIR/src/dashboard/static/css/input.css"
OUTPUT="$ROOT_DIR/src/dashboard/static/css/output.css"

if [ ! -f "$TAILWIND" ]; then
  echo "⚠ Tailwind CLI not found — skipping CSS check"
  exit 0
fi
if [ ! -f "$INPUT" ]; then
  exit 0
fi

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT
"$TAILWIND" -i "$INPUT" -o "$TMPFILE" --minify 2>/dev/null

if [ ! -f "$OUTPUT" ]; then
  echo "❌ output.css missing. Run: bash scripts/build-css.sh"
  exit 1
fi
if ! diff -q "$OUTPUT" "$TMPFILE" > /dev/null 2>&1; then
  echo "❌ output.css is stale. Run: bash scripts/build-css.sh"
  exit 1
fi
