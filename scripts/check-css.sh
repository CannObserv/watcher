#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
INPUT="$ROOT_DIR/src/dashboard/static/css/input.css"
OUTPUT="$ROOT_DIR/src/dashboard/static/css/output.css"
VENDOR_DIR="$ROOT_DIR/src/dashboard/static/css/vendor"

if ! command -v tailwindcss &>/dev/null; then
  echo "Error: tailwindcss not found. Run: sudo npm install -g @tailwindcss/cli"
  exit 1
fi
if [ ! -f "$INPUT" ]; then
  exit 0
fi

# See build-css.sh for why NODE_PATH is set here.
_npm_global="$(npm root -g)" || { echo "Error: 'npm root -g' failed. Is npm installed?"; exit 1; }
export NODE_PATH="$_npm_global/@tailwindcss/cli/node_modules${NODE_PATH:+:$NODE_PATH}"

TMPFILE=$(mktemp)
TMPDIR_LAYERED=$(mktemp -d)
trap 'rm -f "$TMPFILE"; rm -rf "$TMPDIR_LAYERED"' EXIT
tailwindcss -i "$INPUT" -o "$TMPFILE" --minify 2>/dev/null

if [ ! -f "$OUTPUT" ]; then
  echo "❌ output.css missing. Run: bash scripts/build-css.sh"
  exit 1
fi
if ! diff -q "$OUTPUT" "$TMPFILE" > /dev/null 2>&1; then
  echo "❌ output.css is stale. Run: bash scripts/build-css.sh"
  exit 1
fi

# Verify each vendor/*.layered.css matches a fresh wrap of its *.min.css
# source. See docs/STYLE.md §11 (Overriding Vendored CSS).
shopt -s nullglob
for src in "$VENDOR_DIR"/*.min.css; do
  base="$(basename "$src" .min.css)"
  layered="$VENDOR_DIR/$base.layered.css"
  fresh="$TMPDIR_LAYERED/$base.layered.css"
  python3 "$SCRIPT_DIR/wrap-vendor-css.py" "$src" "$fresh"
  if [ ! -f "$layered" ]; then
    echo "❌ $layered missing. Run: bash scripts/build-css.sh"
    exit 1
  fi
  if ! diff -q "$layered" "$fresh" > /dev/null 2>&1; then
    echo "❌ $layered is stale. Run: bash scripts/build-css.sh"
    exit 1
  fi
done
shopt -u nullglob
