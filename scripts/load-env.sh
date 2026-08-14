#!/usr/bin/env bash
# load-env.sh — load watcher's two env files into the CURRENT shell.
#
#   source scripts/load-env.sh
#
# Sourced, never executed: the exports have to land in the caller's shell.
#
# This replaces the idiom that used to appear in AGENTS.md and every runbook:
#
#   export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)     # do not use
#
# which had three defects (gregoryfoster/skills#144):
#   - With both files absent the substitution is empty and it degrades to a
#     bare `export`, printing every exported variable — secrets included —
#     into the transcript.
#   - A `#` comment line reaches `export` as `'#': not a valid identifier`,
#     so under `set -e` it kills the caller before the real command runs.
#   - `xargs` word-splits `PW=two words` into a wrong value, exit 0.
#
# Files load in the order AGENTS.md documents — system first, repo second, so
# a repo-local value wins:
#   1. /etc/watcher/.env       production secrets, managed on the VM
#   2. $PROJECT_ROOT/.env      dev/agent secrets, git-ignored
#
# Both paths are overridable (WATCHER_SYSTEM_ENV_FILE / WATCHER_PROJECT_ENV_FILE)
# so tests never touch the real files. Guarded by tests/scripts/test_load_env.py.

# Parse one env file and export what it defines. Never sources it: a secrets
# file is data, and executing it would run any command substitution inside.
# A malformed line is skipped rather than fatal — a bad line in a secrets file
# must not decide whether the caller's command runs.
load_env_file() {
  local file="$1" line key val
  [ -r "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line#"${line%%[![:space:]]*}"}        # drop leading blanks
    case $line in ''|\#*) continue ;; esac        # blank or comment
    line=${line#export }                          # tolerate `export K=v`
    case $line in *=*) ;; *) continue ;; esac
    key=${line%%=*} val=${line#*=}
    key=${key%"${key##*[![:space:]]}"}            # drop trailing blanks on key
    case $key in ''|*[!A-Za-z0-9_]*) continue ;; esac
    case $val in                                  # strip matched quotes
      \"*\") val=${val#\"} val=${val%\"} ;;
      \'*\') val=${val#\'} val=${val%\'} ;;
    esac
    export "$key=$val"                            # quoted: spaces/globs survive
  done < "$file"
}

load_env_file "${WATCHER_SYSTEM_ENV_FILE:-/etc/watcher/.env}"
load_env_file "${WATCHER_PROJECT_ENV_FILE:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.env}"

# End on a success so that sourcing under `set -e` cannot kill the caller when
# the last file happened to be absent.
:
