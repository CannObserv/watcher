# Collapse WatchedItem ↔ Watch to a single monitored entity

**Date:** 2026-06-16
**Status:** Approved direction (design) — implementation plan to schedule
**Tracking:** (GH issue below)

## Problem

The `WatchedItem` → `Watch` one-to-many was designed (#160, InfoItem-first) when
each `Watch` carried a **`target_info_source_id`** and monitored a *distinct
aspect* of one InfoItem — the primary page, a `cross_check`, or a `sub_aspect`
fragment. One fetch fanned out to N Watches, each watching a **different
fingerprint** of the same bytes. That was a real 1:N: N genuinely-different
monitored things.

**#185 Phase A removed that distinction.** `target_info_source_id` was dropped
from `Watch`; `source_specs` and a single `archiver_info_source_id` moved up to
`WatchedItem`. The pipeline now does **one extraction → one fingerprint → one
`ChangeRevision` per WatchedItem** (`pipeline.py`), then dispatches the
**identical** `CHANGE_DETECTED` (same `change_revision_id`) to every child Watch.

Consequence: a `Watch` no longer represents a distinct monitored target. Every
sibling Watch reacts to the same change signal. The split now carries only:

| Per-Watch feature | Still weight-bearing? |
|---|---|
| Notification routing (`watch_notification_configs`, `watch_nc_refs`) | Overlaps `WatchedItemNotificationTemplate` (WI-level fan-out already exists) |
| Temporal profiles (`temporal_profiles`, per-Watch scheduling) | Thin; 0 rows in prod; could hang off WatchedItem |
| `content_type` / `tags` (resolution chain) | Exists only because there are two levels |
| `name` / `description` / lifecycle flags | Duplicate the WatchedItem's; pause/archive/suspend already cascade between the two |

This also produced the "watches aren't executing — no audit/revisions at the
Watch level" symptom: there is no per-Watch execution to surface anymore. The
check runs per WatchedItem; the Watch has nothing of its own.

## Approach

Collapse to a single monitored entity: **fold `Watch` into `WatchedItem`** (1:1,
then drop `Watch`). `WatchedItem` becomes *the* monitored thing — URL,
source_specs, schedule, health, tags, content_type, notification templates, and
lifecycle all on one row. Delete the `Watch` model/table, the resolution chain
(`src/core/watches/resolution.py`), the dual-level notification tiers, and the
inter-level lifecycle cascades.

Pre-production data is trivial (1 WatchedItem / 1 Watch), so the migration can
truncate rather than backfill — the same posture #160 took.

## What moves / is deleted

- **Temporal profiles** → re-key `temporal_profiles.watch_id` to
  `watched_item_id`; scheduler reads them per WatchedItem.
- **Notification routing** → `watch_notification_configs` / `watch_nc_refs`
  collapse into the WatchedItem notification surface (`WatchedItemNotificationTemplate`
  + a WatchedItem-level config equivalent). The five-tier dispatch in `notify.py`
  loses the per-Watch tier; resolution becomes WatchedItem default → system default.
- **`content_type` / `tags`** → already have WatchedItem defaults; drop the
  override layer and the resolution chain.
- **Lifecycle** → `is_active` / `archived_at` / `domain_suspended` already live on
  WatchedItem; drop `Watch.is_active` / `is_archived` / `suspended_by_domain` and
  the cascade code (incl. the re-probe cascade added in #190).
- **Pipeline / scheduler** → already WatchedItem-driven; remove the child-Watch
  fan-out loop (dispatch once per WatchedItem).
- **API / dashboard** → retire `/watches*` routes, templates, and the Watch-create
  flow; the WatchedItem *is* the watch. Audit/activity already surfaced on
  WatchedItem detail (#190 interim).

## Tradeoffs / alternatives

- **Keep the split, reframe `Watch` as a "notification/schedule subscription"** —
  rejected as the primary path: preserves duplicated lifecycle, the resolution
  chain, and the activity-surfacing seam, for a 1:N capability that overlaps the
  WatchedItem template tier. May be worth revisiting only if a concrete need for
  multiple independent schedules/routes over one URL emerges.
- **Status quo** — rejected: ongoing complexity tax (two lifecycles, two
  notification tiers, resolution chain) with no behavioural payoff post-#185.

## Phased plan (TDD throughout; pre-prod truncating migration)

1. **Notifications** — move per-Watch notification config/refs onto the WatchedItem
   surface; collapse `notify.py` dispatch to WatchedItem-only tiers; update the
   dashboard notification panel (drop the per-Watch tier).
2. **Temporal profiles** — re-key to `watched_item_id`; update scheduler + API + UI.
3. **Pipeline/scheduler** — remove the child-Watch fan-out; dispatch once per
   WatchedItem; carry watch-ish metadata (name/tags) from the WatchedItem.
4. **Model/migration** — drop the `Watch` table and the now-dead columns; truncating
   migration; recreate any triggers in `tests/conftest.py`.
5. **API/dashboard cleanup** — retire `/watches*` routes/templates/schemas and the
   Watch-create flow; redirect or remove nav.
6. **Docs** — rewrite the AGENTS.md "Watches" section around the single entity.

## Out of scope / open questions

- **Naming.** Keep `WatchedItem`, or rename the merged entity to `Watch`? The
  user-facing noun is "Watched Item"; a rename touches a lot. Default: keep
  `WatchedItem`, drop `Watch`.
- **Multiple schedules per URL.** If ever needed, that's a `TemporalProfile`
  list on the WatchedItem, not a separate entity — note for the future.
- The historical procrastinate failure backlog (~8.6k) is a separate
  investigation, not part of this collapse.
