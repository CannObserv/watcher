#!/usr/bin/env bash
# Touch-target / button min-height idiom guard (#203).
#
# The single rule (see docs/STYLE.md "Touch targets"): component classes
# (.btn*, .segment, .chip, .form-input, .toggle, nav-link) own the WCAG 2.1 AA
# 44px min-height. Never restate min-h-[44px] on a .btn (redundant) and never
# use min-h-0 to shrink a component target below 44px (latent a11y bug). Use
# explicit min-h-[44px] ONLY on bare interactive elements (<a>, <label>,
# component-less <button>).
#
# Mirrors tests/dashboard/test_touch_targets.py for shell/CI use.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TPL_DIR="$ROOT_DIR/src/dashboard/templates"
fail=0

# Rule 1: no min-h-0 (shrinks a component target below 44px)
if matches=$(grep -rn "min-h-0" "$TPL_DIR" 2>/dev/null); then
  echo "❌ min-h-0 found (shrinks targets below 44px):"
  echo "$matches"
  fail=1
fi

# Rule 2: no redundant min-h-[44px] restated on a .btn element.
# Per-line check: assumes the `btn` token and any `min-h-[44px]` share one
# physical line (the codebase convention); a .btn split across lines with
# min-h-[44px] on a continuation line would not be flagged.
if matches=$(grep -rn "min-h-\[44px\]" "$TPL_DIR" 2>/dev/null | grep -E "\bbtn\b"); then
  echo "❌ Redundant min-h-[44px] on a .btn element (the .btn class already guarantees 44px):"
  echo "$matches"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "Touch-target rule (docs/STYLE.md): component classes own 44px; use explicit"
  echo "min-h-[44px] ONLY on bare interactive elements (<a>, <label>, component-less <button>)."
  exit 1
fi

echo "✓ touch-target idiom OK"
