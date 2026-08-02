#!/usr/bin/env bash
# End-to-end smoke walkthrough: Archiver InfoItem -> Watcher WatchedItem -> check.
#
# Exercises the live acquisition path against running services: creates an
# InfoItem + bound InfoSource in Archiver, creates the matching WatchedItem in
# Watcher, triggers an immediate check, and polls until the check lands.
#
# No Redis involved — Archiver operates the change bus and Watcher neither
# produces to nor consumes from it (archiver#109; see AGENTS.md).
#
# Requires:
#   - Archiver running on $ARCHIVER_BASE_URL (default http://localhost:8020).
#     If not installed as systemd, start its dev server from the sibling repo:
#       uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8021 --reload &
#       (then set ARCHIVER_BASE_URL=http://localhost:8021)
#   - Watcher running on $WATCHER_BASE_URL (default http://localhost:8001, the
#     dev server; http://localhost:8000 is systemd/prod).
#   - ARCHIVER_API_KEY env var (X-API-Key for the Archiver API).
#   - WATCHER_API_KEY env var (X-API-Key for the Watcher API).
#   - jq.
set -euo pipefail
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)

ARCHIVER_URL="${ARCHIVER_BASE_URL:-http://localhost:8020}"
WATCHER_URL="${WATCHER_BASE_URL:-http://localhost:8001}"
TARGET_URL="${SMOKE_TARGET_URL:-https://example.com}"
: "${ARCHIVER_API_KEY:?ARCHIVER_API_KEY required}"
: "${WATCHER_API_KEY:?WATCHER_API_KEY required for smoke}"

SPECS='[{"schema_version": 1, "extraction": {"algorithm": "full_page"}, "fingerprint": {}}]'

# 1. Create the InfoItem and its primary InfoSource atomically in Archiver.
ITEM_JSON=$(jq -n --arg url "$TARGET_URL" --argjson specs "$SPECS" \
    '{name: "Pipeline smoke", initial_url: $url, initial_source_specs: $specs}' |
    curl -fsS -X POST \
        -H "X-API-Key: $ARCHIVER_API_KEY" \
        -H "content-type: application/json" \
        --data-binary @- \
        "$ARCHIVER_URL/api/v1/info-items")

ITEM=$(jq -r .info_item_id <<<"$ITEM_JSON")
SOURCE=$(jq -r '.info_item_sources[0].info_source_id' <<<"$ITEM_JSON")
echo "Archiver: InfoItem $ITEM with InfoSource $SOURCE"

# 2. Create the WatchedItem in Watcher, bound to both.
WI=$(jq -n --arg item "$ITEM" --arg source "$SOURCE" --arg url "$TARGET_URL" \
    --argjson specs "$SPECS" \
    '{name: "Pipeline smoke", archiver_info_item_id: $item,
      archiver_info_source_id: $source, url: $url, source_specs: $specs}' |
    curl -fsS -X POST \
        -H "X-API-Key: $WATCHER_API_KEY" \
        -H "content-type: application/json" \
        --data-binary @- \
        "$WATCHER_URL/api/v1/watched-items" | jq -r .id)
echo "Watcher:  WatchedItem $WI"

# 3. Trigger an immediate check (202 — the task runs on the embedded worker).
curl -fsS -X POST -H "X-API-Key: $WATCHER_API_KEY" \
    "$WATCHER_URL/api/v1/watched-items/$WI/check-now" >/dev/null
echo "Watcher:  check-now accepted; polling for the result..."

# 4. Poll until the check lands (or give up after ~60s).
CHECKED=""
for _ in $(seq 30); do
    DETAIL=$(curl -fsS -H "X-API-Key: $WATCHER_API_KEY" \
        "$WATCHER_URL/api/v1/watched-items/$WI")
    CHECKED=$(jq -r '.last_checked_at // empty' <<<"$DETAIL")
    [ -n "$CHECKED" ] && break
    sleep 2
done

if [ -z "$CHECKED" ]; then
    echo "FAIL: no check recorded after 60s — is the Watcher worker running?" >&2
    exit 1
fi

echo "Watcher:  checked at $CHECKED, health=$(jq -r .health_status <<<"$DETAIL")"
echo
echo "Audit trail for this item:"
echo "    curl -sS -H \"X-API-Key: \$WATCHER_API_KEY\" \\"
echo "        \"$WATCHER_URL/api/v1/audit?watched_item_id=$WI\" | jq ."
echo
echo "A detected change POSTs a SourceRevision to Archiver inline; failures queue"
echo "in pending_archiver_sync and retry on the 1-minute drain."
