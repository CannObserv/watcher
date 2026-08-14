#!/usr/bin/env bash
# pre-ship.sh — watcher's env-loading wrapper around the vendored
# shipping-work-python-fastapi ship gate.
#
# watcher's conftest and integration fixtures read live secrets, so the gate
# needs /etc/watcher/.env (system) and $PROJECT_ROOT/.env (repo-local) in the
# environment before it runs. Upstream ships without env loading and documents
# this wrapper as the supported override point: SKILL.md Step 1 probes
# `scripts/` first, finds this file, and this file delegates back to the
# vendored gate. Do NOT fork the gate itself — a fork copies every check to add
# a handful of lines, then stops receiving upstream fixes without saying so.
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

# Load secrets through the shared loader — it parses each file rather than
# sourcing it, so a secrets file is never executed, and a malformed line is
# skipped rather than deciding whether the ship gate runs.
# Guarded by tests/scripts/test_load_env.py.
# shellcheck source=scripts/load-env.sh
source "$PROJECT_ROOT/scripts/load-env.sh"

# exec so the exit code the Iron Law gates on propagates unchanged;
# "$@" so --help still reaches the gate.
exec bash "$DELEGATE" "$@"
