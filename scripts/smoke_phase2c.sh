#!/usr/bin/env bash
# Smoke walkthrough for Phase 2c — exercises Information service + Watcher API + Redis.
#
# Requires:
#   - Information service running on $ARCHIVER_BASE_URL (default http://localhost:8020).
#     If not installed as systemd, start dev server:
#       uv run uvicorn src.information.api.main:app --host 0.0.0.0 --port 8020 --reload &
#   - Watcher running on http://localhost:8001 (dev server) or http://localhost:8000 (systemd).
#   - Redis running on $REDIS_URL.
#   - ARCHIVER_API_KEY env var (matches the X-API-Key header the Information service requires).
#   - WATCHER_API_KEY env var (matches the X-API-Key header the Watcher API requires).
set -euo pipefail
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)

INFO_URL="${ARCHIVER_BASE_URL:-http://localhost:8020}"
WATCHER_URL="${WATCHER_BASE_URL:-http://localhost:8001}"

# 1. Create InfoItem + InfoSpec via the Information service
ITEM=$(curl -fsS -X POST -H "X-API-Key: ${ARCHIVER_API_KEY:?ARCHIVER_API_KEY required}" \
    -H "content-type: application/json" \
    -d '{"name": "Smoke 2c"}' \
    "$INFO_URL/api/v1/info-items" | jq -r .info_item_id)

curl -fsS -X POST -H "X-API-Key: $ARCHIVER_API_KEY" \
    -H "content-type: application/json" \
    -d "{\"document\": {\"schema_version\": 1, \"target\": {\"url\": \"https://example.com\"}, \"extraction\": {\"algorithm\": \"full_page\"}, \"fingerprint\": {\"algorithm\": \"simhash\"}}}" \
    "$INFO_URL/api/v1/info-items/$ITEM/info-specs" >/dev/null

# 2. Create a Watch referencing it
WATCH=$(curl -fsS -X POST \
    -H "X-API-Key: ${WATCHER_API_KEY:?WATCHER_API_KEY required for smoke}" \
    -H "content-type: application/json" \
    -d "{\"name\": \"Smoke 2c\", \"info_item_id\": \"$ITEM\", \"content_type\": \"html\"}" \
    "$WATCHER_URL/api/v1/watches" | jq -r .id)

echo "Smoke 2c: created watch $WATCH linked to info_item $ITEM"
echo
echo "To verify the bus end-to-end, run in another shell:"
echo "    uv run python tools/info_changes_consumer.py --group smoke --max-messages 1"
echo "Then trigger a check to produce a change."
