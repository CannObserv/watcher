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

# Parse each env file line by line; never `set -a; . file` (which executes the
# file) and never `export $(cat ... | xargs)` (which word-splits values, chokes
# on comment lines, and dumps the whole environment when every file is absent).
# A malformed line is skipped, not fatal: a bad line in a secrets file must not
# decide whether the ship gate runs.
load_env() {
  [ -r "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line#"${line%%[![:space:]]*}"}        # drop leading blanks
    case $line in ''|\#*) continue ;; esac        # blank or comment
    line=${line#export }                          # tolerate `export K=v`
    case $line in *=*) ;; *) continue ;; esac
    key=${line%%=*} val=${line#*=}
    key=${key%"${key##*[![:space:]]}"}
    case $key in ''|*[!A-Za-z0-9_]*) continue ;; esac
    case $val in                                  # strip matched quotes
      \"*\") val=${val#\"} val=${val%\"} ;;
      \'*\') val=${val#\'} val=${val%\'} ;;
    esac
    export "$key=$val"
  done < "$1"
}

load_env /etc/watcher/.env
load_env "$PROJECT_ROOT/.env"

# exec so the exit code the Iron Law gates on propagates unchanged;
# "$@" so --help still reaches the gate.
exec bash "$DELEGATE" "$@"
