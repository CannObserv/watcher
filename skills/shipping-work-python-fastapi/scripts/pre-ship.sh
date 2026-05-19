#!/usr/bin/env bash
# pre-ship.sh — watcher-specific wrapper around the upstream
# shipping-work-python-fastapi/scripts/pre-ship.sh. Sources /etc/watcher/.env
# (system secrets) and $PROJECT_ROOT/.env (repo-local overrides) before
# delegating to the upstream variant.
set -euo pipefail
PROJECT_ROOT=$(git rev-parse --show-toplevel)

# Use `set -a; source; set +a` instead of `export $(cat | xargs)` to handle
# values containing spaces, quotes, newlines, or `=` correctly. Existence
# guards prevent failure when only one (or neither) env file is present.
if [[ -f /etc/watcher/.env ]]; then
  set -a; source /etc/watcher/.env; set +a
fi
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a; source "$PROJECT_ROOT/.env"; set +a
fi

exec bash "$PROJECT_ROOT/skills-vendor/gregoryfoster-skills/skills/shipping-work-python-fastapi/scripts/pre-ship.sh" "$@"
