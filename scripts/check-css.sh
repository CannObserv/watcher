#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
INPUT="$ROOT_DIR/src/dashboard/static/css/input.css"
OUTPUT="$ROOT_DIR/src/dashboard/static/css/output.css"
VENDOR_DIR="$ROOT_DIR/src/dashboard/static/css/vendor"

# The pinned CLI: one version with build-css.sh, ci.yml and AGENTS.md
# (tests/dashboard/test_css_sources.py holds them together).
TAILWIND_CLI_VERSION="4.2.4"

if ! command -v tailwindcss &>/dev/null; then
  echo "Error: tailwindcss not found. Run: sudo npm install -g @tailwindcss/cli@4.2.4"
  exit 1
fi
# Another version builds a different output.css; say so rather than "stale",
# which would send the reader to rebuild with the wrong CLI. With `CI` set (as
# on GitHub) the banner is coloured even off a TTY, so strip escapes; and Node
# may print a warning first, so take the first line naming a version.
found="$(tailwindcss --help 2>&1 | sed -e 's/\x1b\[[0-9;]*m//g' \
  | sed -n -e 's/.*tailwindcss v\([0-9][0-9.]*\).*/\1/p' | head -n 1)" || true
if [ "$found" != "$TAILWIND_CLI_VERSION" ]; then
  echo "❌ tailwindcss v${found:-unknown} found, pinned v$TAILWIND_CLI_VERSION. Run: sudo npm install -g @tailwindcss/cli@$TAILWIND_CLI_VERSION"
  exit 1
fi
if [ ! -f "$INPUT" ]; then
  exit 0
fi

# See build-css.sh for why NODE_PATH is set here.
_npm_global="$(npm root -g)" || { echo "Error: 'npm root -g' failed. Is npm installed?"; exit 1; }
export NODE_PATH="$_npm_global/@tailwindcss/cli/node_modules${NODE_PATH:+:$NODE_PATH}"

TMPFILE=$(mktemp)
ERRFILE=$(mktemp)
TMPDIR_LAYERED=$(mktemp -d)
trap 'rm -f "$TMPFILE" "$ERRFILE"; rm -rf "$TMPDIR_LAYERED"' EXIT
# stderr carries the CLI's banner on success, so keep it only for a failure.
if ! tailwindcss -i "$INPUT" -o "$TMPFILE" --minify 2>"$ERRFILE"; then
  echo "❌ tailwindcss build failed:"
  cat "$ERRFILE"
  exit 1
fi

if [ ! -f "$OUTPUT" ]; then
  echo "❌ output.css missing. Run: bash scripts/build-css.sh"
  exit 1
fi
if ! diff -q "$OUTPUT" "$TMPFILE" > /dev/null 2>&1; then
  echo "❌ output.css is stale. Run: bash scripts/build-css.sh"
  exit 1
fi

# Verify each vendor/*.layered.css matches a fresh wrap of its *.min.css
# source. See docs/STYLE.md §11 (Overriding Vendored CSS). The layered files
# are git-ignored build products: a clean checkout (CI, a fresh clone) has none
# yet, which is not staleness (#352). Only a present-but-different one fails.
shopt -s nullglob
for src in "$VENDOR_DIR"/*.min.css; do
  base="$(basename "$src" .min.css)"
  layered="$VENDOR_DIR/$base.layered.css"
  fresh="$TMPDIR_LAYERED/$base.layered.css"
  [ -f "$layered" ] || continue
  python3 "$SCRIPT_DIR/wrap-vendor-css.py" "$src" "$fresh"
  if ! diff -q "$layered" "$fresh" > /dev/null 2>&1; then
    echo "❌ $layered is stale. Run: bash scripts/build-css.sh"
    exit 1
  fi
done
shopt -u nullglob
