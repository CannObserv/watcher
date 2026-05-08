#!/bin/bash
# Once-per-day skills submodule refresh. Auto-commits only on main. See #153.
set -u

gitdir="$(git rev-parse --git-dir 2>/dev/null)" || exit 0
LOCK="$gitdir/skills-update-$(date +%Y%m%d)"
LOG="$gitdir/skills-update.log"

# Bound the log: keep the last 200 lines once it crosses 64 KiB.
[ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 65536 ] \
  && tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

[ -f "$LOCK" ] && exit 0

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null)"
[ "$BRANCH" = "main" ] || exit 0

if ! {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] submodule update:"
  git submodule update --remote --merge \
    skills-vendor/gregoryfoster-skills \
    skills-vendor/obra-superpowers 2>&1
} >>"$LOG"; then
  echo "skills update failed (see $LOG)" >&2
  exit 0
fi

touch "$LOCK"

if ! git diff --quiet HEAD \
      skills-vendor/gregoryfoster-skills \
      skills-vendor/obra-superpowers 2>/dev/null; then
  {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] commit submodule bump:"
    git add skills-vendor/gregoryfoster-skills skills-vendor/obra-superpowers \
      && git commit -m 'chore: update skills submodules' 2>&1
  } >>"$LOG"
fi
