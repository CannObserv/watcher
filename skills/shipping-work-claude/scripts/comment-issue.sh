#!/usr/bin/env bash
# comment-issue.sh <issue-number> <comment-body>
# Posts a comment to a GitHub issue via gh CLI.
#
# Usage: bash scripts/comment-issue.sh [--help]
#        bash scripts/comment-issue.sh <number> <body>
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  echo "Usage: bash scripts/comment-issue.sh <issue-number> <comment-body>"
  echo ""
  echo "Posts a comment to a GitHub issue via the gh CLI."
  exit 0
fi

ISSUE="${1:-}"
BODY="${2:-}"

if [ -z "$ISSUE" ] || [ -z "$BODY" ]; then
  echo "Error: issue number and comment body are required."
  echo "Run with --help for usage."
  exit 1
fi

gh issue comment "$ISSUE" --body "$BODY"
echo "Comment posted to issue #$ISSUE."
