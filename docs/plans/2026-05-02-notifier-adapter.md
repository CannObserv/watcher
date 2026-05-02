# Notifier Adapter — Implementation Plan

**Issue:** #132  
**Date:** 2026-05-02  
**Status:** Phase 1 landed (flag-off); Phase 3 (backfill) ready to run on demand.

---

## Decisions

| Question | Decision |
|---|---|
| SDK location | Option A — `clients/python/` in [CannObserv/notifier](https://github.com/CannObserv/notifier); install via `uv add` at a pinned tag |
| Template style | Inline always — watcher pre-renders title/body with its own Jinja engine, passes rendered strings as `title_template`/`body_template` to notifier with `variables={}` |
| `WatchNotificationConfig.events` | Stays in watcher for v0; v1 may push subscriptions to notifier |
| Retry policy | Notifier SDK handles retries (3× exponential backoff, 429 honor). Watcher treats failure as logged DispatchResult. No additional watcher-side retry in Phase 1. |
| Rollback | Flag-flip alone: set `USE_REMOTE_NOTIFY=0` in `/etc/watcher/.env` and restart. Local Apprise path is preserved until Phase 5. |

---

## Architecture

```
src/core/notifier_client/
  __init__.py          re-exports get_notifier_client, build_idempotency_key
  client.py            env-configured NotifierClient factory + idempotency key builder

src/core/notifications/notify.py
  DispatchCandidate    + remote_channel_id field
  _dispatch_via_notifier()   calls notifier API with pre-rendered title/body
  dispatch_event_notifications()
    use_remote = os.getenv("USE_REMOTE_NOTIFY", "0") == "1"
    if use_remote and candidate.remote_channel_id → _dispatch_via_notifier
    else → local dispatch_event (unchanged path)

scripts/migrate_channels_to_notifier.py
  One-shot backfill: decrypt Apprise URL → POST /api/v1/channels → persist remote_channel_id

alembic/versions/96641996744b_*
  Adds remote_channel_id VARCHAR(26) nullable to:
    - notification_templates
    - watch_notification_configs
```

### Idempotency key scheme

| Event type | Key format |
|---|---|
| `change_detected` | `watcher:change_detected:{source_id}:{change_id}` |
| all others | `watcher:{event_type}:{source_id}:{watch_id}:{occurred_at_ms}` |

Keyed per source so a template and a local config that both fire for the same event produce separate notifier dispatch records.

### Audit trail

`notify.py` writes `AuditLog(EventType.NOTIFICATION_DISPATCHED)` with a `results` list as before. When the remote path is taken, each result's `reason` is `notifier:{dispatch_id}` on success, enabling correlation to notifier's own dispatch log.

---

## Phase task list

### Phase 1 — Adapter behind flag (DONE)
- [x] Add `notifier-client @ v0.2.1` to `pyproject.toml`
- [x] `src/core/notifier_client/` module (factory, idempotency key builder)
- [x] `remote_channel_id` column on both notification tables + migration
- [x] `_dispatch_via_notifier` in `notify.py`
- [x] `USE_REMOTE_NOTIFY` flag (default `0`) in dispatch loop
- [x] Unit tests for `notifier_client` module (7 tests)
- [x] Unit tests for remote dispatch path (5 tests)

Default: `USE_REMOTE_NOTIFY` is unset / `"0"` — live service is unaffected.

### Phase 2 — Parallel run (optional, skip for v0)
Dispatch via local Apprise **and** notifier (to a sink/test channel) to compare results before cutover. Not needed given the SDK is well-tested and the notifier service is already stable.

### Phase 3 — Backfill `remote_channel_id`
Run once when ready to cut over:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run python scripts/migrate_channels_to_notifier.py --dry-run   # review
uv run python scripts/migrate_channels_to_notifier.py             # execute
```

Exits 0 if all rows migrated, 1 if any skipped (inspect logs).

### Phase 4 — Flag flip
```bash
# /etc/watcher/.env
USE_REMOTE_NOTIFY=1
NOTIFIER_BASE_URL=http://localhost:9000
NOTIFIER_API_KEY=nk_<key>
```

Then:
```bash
sudo systemctl restart watcher
```

Monitor `sudo journalctl -u watcher -f` for `notification sent` / `notifier error` log lines. Verify notifier dispatch log at `GET /api/v1/dispatch` accumulates expected records.

**Rollback:** Set `USE_REMOTE_NOTIFY=0` and restart. No DB changes needed.

### Phase 5 — Strip local Apprise (future)
After a soak window (≥1 week of Phase 4 without incidents):
- Remove `dispatch_event` call path from `notify.py`
- Remove `apprise` and `cryptography` from `pyproject.toml`
- Drop `apprise_url` columns from `watch_notification_configs` and `notification_templates` (new migration)
- Drop `DispatchCandidate.apprise_url` field
- Remove `src/core/notifications/dispatcher.py` and related tests

File a follow-up issue before starting Phase 5.

---

## Rollback procedure (Phase 4 → Phase 3)

1. `USE_REMOTE_NOTIFY=0` in `/etc/watcher/.env`
2. `sudo systemctl restart watcher`
3. Local Apprise path resumes immediately. No data loss — notifier dispatch records are retained.

If notifier service itself is unavailable and `USE_REMOTE_NOTIFY=1`, the SDK retries (3×) then returns a `NotifierError`. This is caught as a failed dispatch attempt: the audit log records `success=False`, the notification is not delivered, but watcher continues running. The flag-flip restores service instantly.

---

## Environment variables added

| Variable | Required for | Notes |
|---|---|---|
| `NOTIFIER_BASE_URL` | Phase 4+ | `http://localhost:9000` on this VM |
| `NOTIFIER_API_KEY` | Phase 4+ | Watcher tenant key from `scripts/seed_tenant.py` |
| `USE_REMOTE_NOTIFY` | Phase 4+ | `"1"` to enable; default `"0"` |
