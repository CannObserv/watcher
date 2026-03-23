# Rate Limiter: Backoff Decay, Config Reload, Dashboard View

**Issue:** #32
**Date:** 2026-03-23

## Goal

Close three gaps in the rate limiter ↔ domains integration: backoff state never decays, config changes require restart, and operators have no dashboard visibility into per-domain rate state.

## Approved Approach

### 1. Backoff Decay (time-based reset)

**Mechanism:** After a successful fetch, if the domain is in backoff (`current_interval > min_interval`), check whether `now - last_request_at > decay_window`. If so, reset `current_interval` to `min_interval` in both memory and DB.

**Schema change:** New column `decay_window` on `domains` (float, seconds, default 1800.0 / 30 min).

**DomainState changes:** Split the current single `min_interval` field into two:

```python
@dataclass
class DomainState:
    semaphore: asyncio.Semaphore
    last_request_at: float = 0.0
    min_interval: float = DEFAULT_MIN_INTERVAL      # operator floor
    current_interval: float = DEFAULT_MIN_INTERVAL   # effective rate (backoff-adjusted)
    lock: asyncio.Lock
```

- `acquire` / `acquire_for_domain` use `current_interval` for throttling
- `report_rate_limited_for_domain` increases `current_interval`
- New method `reset_domain_interval(domain, min_interval)` resets `current_interval` to `min_interval`
- `configure_domain` updated to accept and store both values

**Where it runs:** In `check_watch` (tasks.py), after a successful fetch — symmetric with the existing 429 path. New helper `_maybe_decay_backoff(domain, session)` reads the Domain row, checks the time condition, resets if met.

### 2. Config Polling (hot-reload)

**Mechanism:** Background `asyncio.Task` polls `domains` every 60s. Query: `SELECT name, max_concurrency, current_interval, min_interval, decay_window FROM domains WHERE updated_at > :last_poll`. For each changed row, call `configure_domain()` to update in-memory state.

**New module:** `src/core/config_poller.py` with `start_config_poller(limiter, session_factory) -> asyncio.Task`.

**Startup integration:** `main.py` `lifespan()` starts the poller after hydration. Cancels it on shutdown alongside the procrastinate worker.

**Worker integration:** Procrastinate workers share the same event loop, so the poller task covers workers in the same process automatically.

**Edge cases:**
- First poll uses startup time as `last_poll` (hydration already loaded everything)
- DB unreachable: log warning, retry next cycle, no crash
- Picks up backoff changes from other processes within 60s

### 3. Dashboard Domains View

**Routes:**
- `GET /dashboard/domains` — full page
- `GET /dashboard/partials/domains` — HTMX partial for auto-refresh

**Data:** Join `domains` table with `COUNT(watches.effective_domain)` for watch count per domain. Merge with `get_domain_states()` for live in-memory backoff status.

**Table columns:** domain name, min_interval, current_interval, decay_window, watch count, last_request_at, backoff status.

**Visual indicators:**
- Row background: red/warm tint when in backoff (`current_interval > min_interval`)
- Backoff status badge: "Active" (red) or "Normal" (green)

**Files:**
- `src/dashboard/context.py` — new query `get_domains_with_watch_counts(session)`
- `src/dashboard/routes.py` — two new route handlers
- `src/dashboard/templates/pages/domains.html` — full page
- `src/dashboard/templates/partials/domains_table.html` — HTMX partial
- Auto-refresh via `hx-trigger="every 30s"` on table container

## Key Decisions

| Decision | Rationale |
|---|---|
| Time-based decay (not per-fetch or N-successes) | Simplest; no per-fetch bookkeeping; decay_window is intuitive for operators |
| 30-min default decay window | Conservative; avoids re-triggering 429 on domains that were recently rate-limited |
| Per-domain configurable decay_window | Different domains have different sensitivity |
| Poll-based config reload (not PG NOTIFY) | Simpler; no dedicated connection management; 60s lag acceptable at current scale |
| Same-process poller covers workers | Procrastinate shares the event loop; multi-process coordination deferred to Redis (see #33) |

## Out of Scope

- Redis-backed shared rate limiting across processes (separate issue)
- Backoff reset UI (operators can PATCH the domain via API to manually reset)
- Per-domain fetch history / request logging
