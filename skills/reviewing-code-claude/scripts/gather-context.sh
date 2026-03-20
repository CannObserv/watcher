#!/usr/bin/env bash
# gather-context.sh
# Collects git and test context for a code review.
# Detects the git project root automatically; safe to invoke from any directory.
#
# Usage: bash skills/reviewing-code-claude/scripts/gather-context.sh [--help]
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  echo "Usage: bash skills/reviewing-code-claude/scripts/gather-context.sh"
  echo ""
  echo "Collects git diff, status, and recent commits for review context."
  exit 0
fi

PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$PROJECT_ROOT"

echo "=== Branch ==="
git branch --show-current

echo ""
echo "=== Status ==="
git status --short

echo ""
echo "=== Staged diff ==="
git diff --staged

echo ""
echo "=== Unstaged diff ==="
git diff

echo ""
echo "=== Recent commits ==="
git log --oneline -10

echo ""
echo "=== Test suite ==="
uv run pytest --no-cov -m "not integration"
