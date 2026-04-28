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

# Tailwind v4's @import "tailwindcss" resolves via Node module resolution from
# the CSS file's directory. With a global CLI install the tailwindcss CSS package
# lives inside the CLI's own node_modules, so we expose it via NODE_PATH.
_npm_global="$(npm root -g)" || { echo "Error: 'npm root -g' failed. Is npm installed?"; exit 1; }
export NODE_PATH="$_npm_global/@tailwindcss/cli/node_modules${NODE_PATH:+:$NODE_PATH}"

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
  tailwindcss -i "$INPUT" -o "$OUTPUT" --watch
else
  tailwindcss -i "$INPUT" -o "$OUTPUT" --minify
fi
