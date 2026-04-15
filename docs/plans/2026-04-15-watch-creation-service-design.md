# Watch Creation Service — Design

**Date:** 2026-04-15
**Issue:** CannObserv/watcher#98

## Goal

Fix the dashboard watch creation route (`POST /watches/new`) which creates Watch records
without probing the URL, leaving `effective_url`/`effective_domain` empty, skipping Domain
upsert, and omitting notification dispatch. Extract a shared service function used by both
the API and dashboard routes.

## Approved Approach: Shared Service Function (Option B)

Extract `create_watch()` into `src/core/watches.py`. Both routes call it; each handles its
own HTTP response or redirect.

### Why not Option A (duplicate logic)?
Two callsites diverge over time — already happened once, causing this bug.

### Why not Option C (internal HTTP)?
Adds a network hop and couples dashboard to API availability with no benefit in a monolith.

## Key Decisions

### Service function signature

```python
async def create_watch(
    session: AsyncSession,
    probe_fn: Callable[[str], Awaitable[ProbeResult]],
    name: str,
    url: str,
    content_type: ContentType,
    schedule_config: dict,
    fetch_config: dict,
) -> Watch:
    ...
```

Returns the committed, refreshed `Watch`. Raises `httpx.HTTPError` on probe failure (caller
converts to appropriate HTTP error or flash message).

### Full creation flow (mirrors existing API route)

1. `probe_fn(url)` — resolve effective URL and domain
2. Domain upsert — insert with defaults if new; IntegrityError-safe for races
3. `Watch` insert + `session.flush()` — get `watch.id` before audit
4. `audit(session, EventType.WATCH_CREATED, watch_id=watch.id, ...)`
5. `dispatch_event_notifications(session, WatchEvent(WATCH_CREATED, ...))`
6. `session.commit()` + `session.refresh(watch)`

### Dashboard error handling

`watch_create_submit` catches `httpx.HTTPError` and re-renders the form with a flash error,
consistent with the API's 422 response.

## Out of Scope

- Backfilling `effective_url`/`effective_domain` on existing broken watches (separate task)
- Changing the API route's HTTP contract
- Any other dashboard form validation improvements
