# Domains Table & Rate Limiter Persistence — Design

**Date**: 2026-03-22
**Status**: Approved
**Issues**: #11 (domains table), #29 (API versioning, separate)

## Goal

Persist per-domain rate limiter configuration and backoff state across restarts. Add a `domains` table, expose it via a CRUD API, and resolve effective URLs at watch creation time to ensure rate limiting is applied against the real destination domain.

## Approved Approach

### Data Model

**`watches`** — two new nullable columns (nullable for backward compat with existing rows):
- `effective_url` — final URL after following the redirect chain, resolved at watch creation
- `effective_domain` — hostname extracted from `effective_url`; used by rate limiter instead of raw URL hostname

Existing watches leave both null; populated on next check run or re-creation. URL field remains immutable — original URL is always preserved even if it redirects to a known effective URL.

**`domains`** — new table:

| Column | Type | Notes |
|---|---|---|
| `id` | ULID PK | |
| `name` | varchar(253) unique not null | effective hostname |
| `min_interval` | float not null | operator-configured floor, default 1.0 |
| `max_concurrency` | int not null | default 2 |
| `current_interval` | float not null | backoff-adjusted, persisted on 429 events only |
| `last_request_at` | datetime nullable | persisted on 429 events only (not every fetch) |
| `created_at`, `updated_at` | datetime | `TimestampMixin` |

`current_interval` is the backoff-adjusted effective interval; `min_interval` is the operator-configured floor. `current_interval` resets to `min_interval` when backoff clears (not yet implemented; future work).

### API Surface

| Endpoint | Behavior |
|---|---|
| `POST /api/probe` | Resolve a URL — returns `effective_url`, `effective_domain`, `redirect_chain`, `status_code`. Public utility; also called internally at watch creation. |
| `POST /api/watches` | Calls probe internally, stores `effective_url`/`effective_domain`, upserts domain record (insert with defaults if new, leave config intact if exists). Fails fast on invalid/unreachable URL. |
| `GET /api/domains` | List all domain configs |
| `GET /api/domains/{name}` | Get one domain config |
| `PATCH /api/domains/{name}` | Upsert — creates with provided config if absent, updates fields if present |
| `DELETE /api/domains/{name}` | 409 Conflict if any watches have `effective_domain = name` |

`POST /api/domains` is intentionally omitted — `PATCH` serves as upsert and is the only creation path for operators. Domain records are primarily auto-created at watch creation time.

### Rate Limiter Integration

`DomainRateLimiter` stays DB-free. Integration at two points:

1. **App startup (lifespan)**: load all `Domain` rows from DB → call `configure_domain(name, min_interval, max_concurrency, current_interval)` on the rate limiter to hydrate in-memory state.

2. **On 429 response**: fetcher calls `report_rate_limited(url)` (updates in-memory state as today), then also upserts `current_interval` + `last_request_at` on the `Domain` row.

Rate limiter `acquire()` uses `watch.effective_domain` rather than extracting hostname from the raw URL.

### Domain Deletion Guard

`DELETE /api/domains/{name}` returns 409 if any `watches` row has `effective_domain = name`. Operator must delete or reassign those watches first. Orphaned domain configs (all watches deleted, domain record remains) are harmless and preserve operator-tuned config for future reuse.

## Key Decisions

- **URL immutability**: `PATCH /api/watches` will not include a `url` field. URL changes require delete + recreate, preserving full audit trail.
- **No `www.` normalization in rate limiter**: probe follows redirects and stores the real effective domain; no hostname heuristics needed.
- **Persist on 429 only**: `last_request_at` and `current_interval` are updated on rate-limit events, not on every fetch. Per-request DB writes would be too expensive at scale.
- **Backfill strategy**: existing watches get null `effective_url`/`effective_domain`; worker populates on first successful fetch. No migration-time backfill.

## Out of Scope

- API versioning (`/api/v1/`) — tracked in #29
- Backoff reset logic (when `current_interval` decays back to `min_interval`) — future work
- Browser-based redirect following (Playwright fetcher) — future work
- `PATCH /api/watches` URL field — intentionally omitted; URL is immutable
