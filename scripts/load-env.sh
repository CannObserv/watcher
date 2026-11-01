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
#
# A linked worktree has no .env of its own: it is git-ignored, so only the
# primary checkout has one. Without an override, a checkout with no .env falls
# back to the primary's, beside the git common dir (#404; git >= 2.31). When
# neither file is readable it WARNs on stderr, naming the paths tried, never a
# value: a silent empty load let the DB tests skip and the gate pass.
#
# The names it set (never the values) are left in WATCHER_LOADED_ENV_NAMES, a
# shell variable it does not export. scripts/pre-ship.sh exports them as the
# vendored gate's PRE_SHIP_ENV_NAMES, its stamp key; an interactive shell keeps
# no gate-only variable in its environment.

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
    WATCHER_LOADED_ENV_NAMES="${WATCHER_LOADED_ENV_NAMES:+$WATCHER_LOADED_ENV_NAMES }$key"
  done < "$file"
}

WATCHER_LOADED_ENV_NAMES=""
_watcher_sys_env=${WATCHER_SYSTEM_ENV_FILE:-/etc/watcher/.env}
if [ -n "${WATCHER_PROJECT_ENV_FILE:-}" ]; then
  _watcher_proj_env=$WATCHER_PROJECT_ENV_FILE
  _watcher_tried=$_watcher_proj_env
else
  _watcher_proj_env="$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.env"
  _watcher_tried=$_watcher_proj_env
  if [ ! -r "$_watcher_proj_env" ] &&
    _watcher_common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null); then
    _watcher_primary_env="$(dirname "$_watcher_common")/.env"
    if [ "$_watcher_primary_env" != "$_watcher_proj_env" ]; then
      _watcher_proj_env=$_watcher_primary_env
      _watcher_tried="$_watcher_tried or $_watcher_proj_env"
    fi
  fi
fi
if [ ! -r "$_watcher_sys_env" ] && [ ! -r "$_watcher_proj_env" ]; then
  echo "WARN: load-env.sh: no readable env file at $_watcher_sys_env or $_watcher_tried;" >&2
  echo "      nothing was loaded" >&2
fi
load_env_file "$_watcher_sys_env"
load_env_file "$_watcher_proj_env"
unset _watcher_sys_env _watcher_proj_env _watcher_tried _watcher_common _watcher_primary_env

# End on a success so that sourcing under `set -e` cannot kill the caller when
# the last file happened to be absent.
:
