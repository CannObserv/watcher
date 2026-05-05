#!/usr/bin/env bash
# Smoke walkthrough for Phase 3a — exercises every authoring tool end-to-end
# against a live Information service.
#
# Reference flow:
#   1. find_info_item (expect empty hit on the smoke name).
#   2. fetch_and_render — fetch a fixture URL, sanity-check the body.
#   3. propose_selectors — get ranked candidates for description.
#   4. validate_info_spec — confirm the assembled doc validates.
#   5. preview_extraction — verify the spec yields chunks against the URL.
#   6. create_info_item with initial_info_spec — atomic create.
#   7. get_primary_info_spec round-trip — confirm the new spec is reachable.
#
# Requires:
#   - Information service on $INFORMATION_BASE_URL (default http://localhost:8020).
#   - INFORMATION_API_KEY in env (loaded automatically below from /etc/watcher/.env + .env).
#   - jq, curl.
#   - Internet egress for the fixture URL (https://example.com).
set -euo pipefail
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)

INFO_URL="${INFORMATION_BASE_URL:-http://localhost:8020}"
KEY="${INFORMATION_API_KEY:?INFORMATION_API_KEY required}"
SMOKE_NAME="Phase 3a Smoke $$"
FIXTURE_URL="https://example.com/"
DESCRIPTION="Example Domain"

call() {
    # call <verb> <path> [<json-body>]
    local verb="$1"
    local path="$2"
    local body="${3:-}"
    if [ -n "$body" ]; then
        curl -fsS -X "$verb" \
            -H "X-API-Key: $KEY" \
            -H "content-type: application/json" \
            -d "$body" \
            "$INFO_URL$path"
    else
        curl -fsS -X "$verb" \
            -H "X-API-Key: $KEY" \
            "$INFO_URL$path"
    fi
}

echo "==> 1. find_info_item: confirm the smoke name doesn't exist yet"
HITS=$(call GET "/api/v1/tools/find-info-items?q=Phase%203a%20Smoke%20$$" | jq 'length')
[ "$HITS" = "0" ] || { echo "expected 0 hits, got $HITS"; exit 1; }

echo "==> 2. fetch_and_render: fetch the fixture URL"
FETCH=$(call POST /api/v1/tools/fetch-and-render "{\"url\": \"$FIXTURE_URL\"}")
echo "$FETCH" | jq -e '.status_code == 200 and (.body | length > 0)' >/dev/null

echo "==> 3. propose_selectors: rank candidates for '$DESCRIPTION'"
CANDIDATES=$(call POST /api/v1/tools/propose-selectors \
    "{\"url\": \"$FIXTURE_URL\", \"description\": \"$DESCRIPTION\"}")
COUNT=$(echo "$CANDIDATES" | jq 'length')
[ "$COUNT" -ge 1 ] || { echo "expected ≥1 candidate, got $COUNT"; exit 1; }
TOP_SELECTOR=$(echo "$CANDIDATES" | jq -r '.[0].selector')
echo "    top candidate: $TOP_SELECTOR"

echo "==> 4. validate_info_spec: confirm the assembled doc validates"
DOC=$(jq -nc \
    --arg url "$FIXTURE_URL" \
    --arg sel "$TOP_SELECTOR" \
    '{schema_version: 1, target: {url: $url}, extraction: {algorithm: "css", selector: $sel}, fingerprint: {algorithm: "simhash"}}')
VALID=$(call POST /api/v1/tools/validate-info-spec "{\"document\": $DOC}" | jq -r .valid)
[ "$VALID" = "true" ] || { echo "validate_info_spec returned $VALID"; exit 1; }

echo "==> 5. preview_extraction: dry-run extraction with the chosen selector"
PREVIEW=$(call POST /api/v1/tools/preview-extraction \
    "{\"url\": \"$FIXTURE_URL\", \"document\": $DOC}")
PREVIEW_CHUNKS=$(echo "$PREVIEW" | jq '.chunks | length')
PREVIEW_FP=$(echo "$PREVIEW" | jq -r .computed_fingerprint)
[ "$PREVIEW_CHUNKS" -ge 1 ] || { echo "expected ≥1 chunk, got $PREVIEW_CHUNKS"; exit 1; }
[ -n "$PREVIEW_FP" ] || { echo "missing computed_fingerprint"; exit 1; }
echo "    preview returned $PREVIEW_CHUNKS chunks, fingerprint=$PREVIEW_FP"

echo "==> 6. create_info_item with initial_info_spec: atomic create"
CREATED=$(call POST /api/v1/info-items \
    "{\"name\": \"$SMOKE_NAME\", \"initial_info_spec\": $DOC}")
ITEM_ID=$(echo "$CREATED" | jq -r .info_item_id)
SPEC_ID=$(echo "$CREATED" | jq -r .info_spec_id)
[ -n "$ITEM_ID" ] && [ "$ITEM_ID" != "null" ] || { echo "missing info_item_id"; exit 1; }
[ -n "$SPEC_ID" ] && [ "$SPEC_ID" != "null" ] || { echo "missing info_spec_id"; exit 1; }
echo "    info_item_id=$ITEM_ID  info_spec_id=$SPEC_ID"

echo "==> 7. get_primary_info_spec: round-trip the new spec"
PRIMARY=$(call GET "/api/v1/info-items/$ITEM_ID/primary-info-spec")
PRIMARY_SPEC_ID=$(echo "$PRIMARY" | jq -r .info_spec_id)
[ "$PRIMARY_SPEC_ID" = "$SPEC_ID" ] || {
    echo "primary spec mismatch: got $PRIMARY_SPEC_ID, expected $SPEC_ID"; exit 1;
}

echo
echo "Phase 3a smoke OK."
echo "    info_item_id=$ITEM_ID"
echo "    info_spec_id=$SPEC_ID"
echo "    fingerprint=$PREVIEW_FP"
