# Domain → WatchedItem Relationship Design

**Date:** 2026-05-26
**Status:** Approved

## Goal

Lift the Domain → WatchedItem relationship to a first-class association. Currently Domains are implicitly linked to Watches via the denormalized `Watch.effective_domain` string, with no awareness of WatchedItems. The natural model is Domain → many WatchedItems, with their child Watches coming along for the ride. This redesign makes that relationship explicit in the data model, cleans up domain suspension cascade logic, and updates the domain detail UI to surface WatchedItems instead of a flat watch list.

## Approved Approach

Add `domain_name` (FK → `Domain.name`) and `domain_suspended` to `WatchedItem`. Remove `Watch.effective_domain` entirely (its role is absorbed by the WatchedItem FK). Keep `Watch.domain_suspended` as the per-watch restoration guard that protects manually-deactivated watches during domain suspension/reactivation.

## Key Decisions and Rationale

**1:1 cardinality on WatchedItem → Domain.** A WatchedItem wraps a single Archiver InfoItem; that InfoItem has one primary URL; that URL resolves to one domain. A WatchedItem never spans multiple domains, so a FK on WatchedItem is correct (not a junction table).

**Explicit FK, not implicit derivation.** Deriving the WatchedItem set for a domain via `JOIN watches ON effective_domain` works but is indirect and can't be indexed directly. A FK on WatchedItem makes the relationship queryable in one hop and is the natural home for the association.

**Remove `Watch.effective_domain`, not just supplement it.** Keeping two sources of truth for domain identity would create drift risk. All callers that need a watch's domain read `watch.watched_item.domain_name` instead.

**Cascade domain_suspended at two levels.** `WatchedItem.domain_suspended` is a domain-level signal ("this WatchedItem's domain is currently suspended") — used for UI banners and domain queries. `Watch.domain_suspended` remains the per-watch restoration guard — only watches that were individually active at suspension time get restored on reactivation, protecting manually-deactivated watches.

**`domain_name` is nullable on WatchedItem.** Standalone WatchedItems created before any Watch (via `POST /api/v1/watched-items`) have no domain yet. Domain is set at first Watch creation time from the probe result.

## Data Model Changes

### `WatchedItem` — new columns

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `domain_name` | `String(253)` FK → `Domain.name` | Yes | Set at Watch-create time; NULL for standalone WIs with no watches |
| `domain_suspended` | `Boolean` | No (default `False`) | Domain-level suspension signal |

### `Watch` — removed column

| Column | Removed |
|---|---|
| `effective_domain` | Absorbed into `WatchedItem.domain_name` |

`Watch.domain_suspended` and `Watch.effective_url` are unchanged.

### Migration (three steps, one revision)

1. Add `watched_items.domain_name` (nullable FK → `Domain.name`) and `watched_items.domain_suspended` (boolean, default `false`)
2. Backfill: `UPDATE watched_items SET domain_name = (SELECT effective_domain FROM watches WHERE watched_item_id = watched_items.id LIMIT 1)`
3. Drop `watches.effective_domain`

## Domain Suspension Cascade

**Deactivation (`POST /domains/{name}/toggle-active`, new_active=False):**
1. Find all non-archived WatchedItems where `domain_name = name` → set `domain_suspended = True`
2. For each: set `is_active = False, domain_suspended = True` on child Watches that were `is_active = True`

**Reactivation (new_active=True):**
1. Find WatchedItems where `domain_name = name AND domain_suspended = True` → clear `domain_suspended`
2. For each: set `is_active = True, domain_suspended = False` on child Watches where `domain_suspended = True` (manually-deactivated watches are untouched)

**Known limitation:** If a Watch is manually deactivated while the domain is already suspended, it will have `domain_suspended = False, is_active = False`. On domain reactivation it will remain inactive (correct). However, if a Watch is manually deactivated *after* domain suspension (while the watch is already `is_active=False, domain_suspended=True`), the `domain_suspended` flag on that watch will be cleared on reactivation and it will be set back to active. This edge case is deferred to a follow-on.

## API Changes

### `WatchResponse` — breaking change
- Remove `effective_domain` field

### `WatchedItemResponse` — additive
- Add `domain_name: str | None`
- Add `domain_suspended: bool`

### `PATCH /api/v1/watches/{id}` — reactivation guard simplified
Old: queries `Domain` via `watch.effective_domain` to check `domain.is_active`.
New: reads `watch.watched_item.domain_suspended` directly — no extra DB query.

### `GET /api/v1/watched-items` — new filter
Add `?domain=<name>` query parameter filtering on `WatchedItem.domain_name`.

## UI / UX Changes

**Navigation flow:**
```
Before:  Domains → Watches (flat table per domain)
After:   Domains → Watched Items (per domain) → Watches (existing /watched-items/{id})
```

**Domain list (`/domains`):** "Watches" count column → "Watched Items" count. Query joins on `WatchedItem.domain_name` instead of `Watch.effective_domain`.

**Domain detail (`/domains/{name}`):**
- Watches table replaced with a Watched Items list
- Columns: Name | Status badge | Watches (count) | Last Checked | → link to `/watched-items/{id}`
- Name search + active/archived filter; sort by name or last-checked
- HTMX partial `/partials/domain-watched-items/{name}` replaces `/partials/domain-watches/{name}`
- Domain toggle OOB response swaps the WatchedItems list
- Config fields and danger zone unchanged

**WatchedItem detail (`/watched-items/{id}`):**
- Add Domain row to the details grid: domain name as a link → `/domains/{name}` (only when `domain_name` is set)
- Show "Domain Inactive" banner when `watched_item.domain_suspended = True`

**Watch detail (`/watches/{id}`):**
- Domain link source: `watch.effective_domain` → `watch.watched_item.domain_name`
- "Domain Inactive" badge source: `watch.domain_suspended` (unchanged — still per-watch flag)

**Watch list (`/watches`):** Domain filter param routes via `WatchedItem.domain_name` join instead of `Watch.effective_domain`.

## Out of Scope

- Pagination on domain-watched-items list (domain WatchedItem counts are small in v1)
- Manual domain assignment UI (domain_name always set automatically at Watch-create time)
- Moving domain suspension to a `Domain.suspended_at` timestamp (deferred)
- Resolving the edge case where a manually-deactivated watch has `domain_suspended=True` set after domain suspension
