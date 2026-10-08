#!/usr/bin/env bash
# pre-ship.sh — watcher's env-loading wrapper around the vendored
# shipping-work-python-fastapi ship gate.
#
# watcher's conftest needs TEST_DATABASE_URL, so the gate needs
# /etc/watcher/.env (system) and $PROJECT_ROOT/.env (repo-local) in the
# environment before it runs. Upstream ships without env loading and documents
# this wrapper as the supported override point: SKILL.md Step 1 probes
# `scripts/` first, finds this file, and this file delegates back to the
# vendored gate. Do NOT fork the gate itself — a fork copies every check to add
# a handful of lines, then stops receiving upstream fixes without saying so.
#
# Then it runs what the vendored gate deselects: `pytest -m integration`
# (#353). watcher merges to main locally and restarts from it, so CI reports
# after the code is live and this is the last gate before deploy. The mark
# needs only the local test database (~20 s); a skip or xfail fails it like a
# failure does (scripts/check_no_skips.py). Guarded by tests/scripts/test_pre_ship.py.
set -euo pipefail
PROJECT_ROOT=$(git rev-parse --show-toplevel)
cd "$PROJECT_ROOT"

# Delegate through the skills/ path, never skills-vendor/ — the symlink is the
# stable interface, the vendor directory layout is not.
DELEGATE="skills/shipping-work-python-fastapi/scripts/pre-ship.sh"
[[ -f "$DELEGATE" ]] || {
  echo "ERROR: vendored gate missing at $DELEGATE" >&2
  echo "       fix: git submodule update --init --recursive" >&2
  exit 2
}

# The vendored gate inserts .skills/pre-ship-uv-args after every `uv run`; the
# integration run below does not (watcher has no such file — docs/SKILLS.md,
# #318). Its arrival would split the two runs into different environments
# without a word, so it stops the gate until this wrapper honours it (#353 CR 3).
if [[ -e .skills/pre-ship-uv-args || -L .skills/pre-ship-uv-args ]]; then
  echo "ERROR: .skills/pre-ship-uv-args exists, but this wrapper's integration run" >&2
  echo "       ignores it. Teach scripts/pre-ship.sh to pass the same arguments first." >&2
  exit 2
fi

# --help: the gate's own text, then what this wrapper adds. Runs nothing.
if [[ "${1:-}" == "--help" ]]; then
  bash "$DELEGATE" "$@"
  echo ""
  echo "watcher wrapper (scripts/pre-ship.sh): loads the env files, runs the gate"
  echo "above, then 'uv run pytest -m integration' and fails on any failure or"
  echo "skip (scripts/check_no_skips.py). Exit code: the first failing step's."
  exit 0
fi

# Load secrets through the shared loader — it parses each file rather than
# sourcing it, so a secrets file is never executed, and a malformed line is
# skipped rather than deciding whether the ship gate runs.
# Guarded by tests/scripts/test_load_env.py.
# shellcheck source=scripts/load-env.sh
source "$PROJECT_ROOT/scripts/load-env.sh"

# Not exec'd any more — the integration run follows. Under `set -e` a failing
# gate still exits with its own code, so the Iron Law sees it unchanged.
bash "$DELEGATE" "$@"

echo ""
echo "=== Integration tests (watcher wrapper, #353) ==="
JUNIT=$(mktemp) || { echo "ERROR: mktemp failed (JUNIT)" >&2; exit 2; }
trap 'rm -f "$JUNIT"' EXIT
uv run pytest -m integration -x -q --no-cov --junitxml="$JUNIT"
uv run python scripts/check_no_skips.py "$JUNIT"
