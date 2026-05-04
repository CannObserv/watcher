#!/usr/bin/env bash
# Regenerate clients/python/src/information_client/generated/ from the Information
# service's OpenAPI schema. Idempotent — safe to run repeatedly.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SDK_DIR="${REPO_ROOT}/clients/python"
GEN_DIR="${SDK_DIR}/src/information_client/generated"

cd "${REPO_ROOT}"
TMP_SPEC="$(mktemp -t information-openapi-XXXXXX.json)"
trap 'rm -f "${TMP_SPEC}"' EXIT
uv run python scripts/dump_openapi_information.py > "${TMP_SPEC}"

cd "${SDK_DIR}"
rm -rf "${GEN_DIR}"
uv run openapi-python-client generate \
    --path "${TMP_SPEC}" \
    --meta none \
    --output-path "${GEN_DIR}" \
    --overwrite

uv run ruff format "${GEN_DIR}" || true   # cosmetic; don't fail regen on format diffs
echo "Regenerated: ${GEN_DIR}"
