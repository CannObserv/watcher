#!/usr/bin/env bash
# Run one uv command with a co-pypi access token in its environment (#358).
#
#   source scripts/load-env.sh          # GOOGLE_APPLICATION_CREDENTIALS
#   bash scripts/co-pypi.sh uv lock
#   bash scripts/co-pypi.sh uv sync --locked
#
# The token is minted by scripts/co_pypi_token.py and exported to the wrapped
# uv alone; it is never printed. --no-project: the project can't be synced to
# run this, since that sync is what needs the token. Details: that module's
# docstring and docs/DEPENDENCIES.md → *The co-pypi index*.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --no-project --with 'google-auth[requests]>=2,<3' \
  python "$HERE/co_pypi_token.py" "$@"
