#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TAILWIND="$SCRIPT_DIR/tailwindcss"
INPUT="$ROOT_DIR/src/dashboard/static/css/input.css"
OUTPUT="$ROOT_DIR/src/dashboard/static/css/output.css"
VENDOR_DIR="$ROOT_DIR/src/dashboard/static/css/vendor"

if [ ! -f "$TAILWIND" ]; then
  echo "Error: Tailwind CLI not found at $TAILWIND"
  exit 1
fi

# Wrap each vendored *.min.css in @layer vendor { ... } so input.css's
# @layer components rules can override them without !important. Idempotent —
# regenerated from source on every build. See docs/STYLE.md "Overriding
# vendored CSS". Logic lives in scripts/wrap-vendor-css.py (also used by
# scripts/check-css.sh for staleness verification).
shopt -s nullglob
for src in "$VENDOR_DIR"/*.min.css; do
  base="$(basename "$src" .min.css)"
  out="$VENDOR_DIR/$base.layered.css"
  python3 "$SCRIPT_DIR/wrap-vendor-css.py" "$src" "$out"
done
shopt -u nullglob

if [ "${1:-}" = "--watch" ]; then
  "$TAILWIND" -i "$INPUT" -o "$OUTPUT" --watch
else
  "$TAILWIND" -i "$INPUT" -o "$OUTPUT" --minify
fi
