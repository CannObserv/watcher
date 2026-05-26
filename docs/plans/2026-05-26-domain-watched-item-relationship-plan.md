---
title: "#177 Domain → WatchedItem first-class relationship"
date: 2026-05-26
status: draft
---

# #177 Domain → WatchedItem First-Class Relationship

## Problem

`Watch.effective_domain` is the only link between a Domain and the rest of the model. It is a raw denormalized string with no FK, and it bypasses WatchedItem entirely — the domain detail page shows a flat watch table rather than the WatchedItems those watches belong to. The domain suspension cascade operates directly on individual Watch rows selected by string-matching `effective_domain`. This produces an inverted ownership model: Domains should reference WatchedItems, with child Watches coming along for the ride.

## Approach

Add `domain_name` (FK → `Domain.name`) and `domain_suspended: bool` to `WatchedItem`. Remove `Watch.effective_domain` entirely — all callers read domain from `watch.watched_item.domain_name` instead. One migration handles the column additions, a single-query backfill, and the drop. The domain suspension cascade becomes Domain → `WatchedItem.domain_suspended` → `Watch.domain_suspended`; the per-Watch flag is kept as the restoration guard for manually-deactivated watches. All `effective_domain` callsites in src/ and tests/ are updated in eight focused steps, each independently runnable and verifiable.

## Tradeoffs / alternatives

- **Implicit derivation (no schema change)** — join WatchedItems through Watches to derive the domain set. Rejected: indirect, can't be indexed, semantically muddy.
- **Junction table** — `domain_watched_items` many-to-many. Rejected: WatchedItems never span multiple domains (one InfoItem, one primary URL, one domain); the extra table is YAGNI.

## Steps

1. **Model + migration** — Add `domain_name` (nullable, FK → `Domain.name`, indexed) and `domain_suspended` (bool, default `false`) to `WatchedItem`. One migration: add columns, backfill `domain_name` from each WatchedItem's first Watch, then drop `watches.effective_domain`. Update `WatchedItem.__init__` default. No source code beyond models and migration in this step. Green: existing model tests still pass; new column presence asserted.

2. **Watch creation + test factory + resolution** — Update `create_watch` (`src/core/watches/__init__.py`) to set `watched_item.domain_name = probe_result.effective_domain` at WatchedItem auto-create time. Update `make_watch` factory (`tests/conftest.py`): rename `effective_domain=` param to `domain_name=` and set it on the WatchedItem instead of the Watch. Update every test call-site that passes `effective_domain=` to `make_watch`. Update `resolution.py` (`watch_event_base_metadata`): read `watch.watched_item.domain_name` instead of `watch.effective_domain` for the `"effective_domain"` event-metadata key (the key name in WatchEvent metadata stays the same — it's a semantic label). Green: `tests/core/test_watches.py`, `tests/test_create_watch_service.py`, `tests/core/test_watches/resolution`.

3. **Domain routes: suspension cascade + delete guard** — Rewrite the domain toggle route (`POST /domains/{name}/toggle-active`, `src/dashboard/routes.py`) to cascade: deactivation sets `WatchedItem.domain_suspended = True` then `Watch.is_active = False, Watch.domain_suspended = True` on active child watches; reactivation reverses only Watch rows with `domain_suspended = True`. Remove all `Watch.effective_domain` references in the toggle block. Update the domain delete guard (`src/api/routes/domains.py`) to check `WatchedItem.domain_name == name` instead of `Watch.effective_domain == name`. Green: `tests/dashboard/test_domain_routes.py` (suspend/restore/delete tests).

4. **Notifications + worker** — `notify.py`: rewrite the watch-meta query (`select(Watch.effective_domain, ...)`) to join WatchedItem and read `WatchedItem.domain_name` instead; update `effective_domain` local variable source. `tasks.py`: replace `{w.effective_domain for w in children}` with `{w.watched_item.domain_name for w in children}` and update the Domain join (`Domain.name == Watch.effective_domain` → `Domain.name == WatchedItem.domain_name`). Green: `tests/workers/test_notify.py`, `tests/workers/test_tasks.py`.

5. **API schemas + PATCH guard + watched-items domain filter** — `WatchResponse`: remove `effective_domain`. `WatchUpdate`: remove `effective_domain` patchable field. `WatchedItemResponse`: add `domain_name: str | None` and `domain_suspended: bool`. `PATCH /api/v1/watches/{id}` reactivation guard: replace Domain lookup via `watch.effective_domain` with `watch.watched_item.domain_suspended` check (no extra DB query). `GET /api/v1/watched-items`: add `?domain=` query param filtering on `WatchedItem.domain_name`. Drop tests for `PATCH effective_domain` directly. Green: `tests/api/test_watches.py`, `tests/api/test_watched_items.py`.

6. **Dashboard context queries + domain list** — `context.py`: rename `get_domain_watches` → `get_domain_watched_items` returning `list[WatchedItem]` with watch counts, joining on `WatchedItem.domain_name`; update `get_domains_with_watch_counts` to join on `WatchedItem.domain_name` and count WatchedItems (not Watches); update watch-list domain filter (`Watch.effective_domain.ilike` → join through WatchedItem). Update the domain list template column header from "Watches" to "Watched Items". Green: `tests/dashboard/test_context.py`, `tests/dashboard/test_routes.py` (domain list).

7. **Domain detail UI — WatchedItems list** — Replace the flat watch table on `/domains/{name}` with a WatchedItems list: columns Name | Status badge | Watches (count) | Last Checked | → `/watched-items/{id}`. New HTMX partial route `GET /partials/domain-watched-items/{name}` (replaces `/partials/domain-watches/{name}`). Update `domain_toggle_oob.html` OOB response to swap the WatchedItems list. Update domain detail template. Green: `tests/dashboard/test_domain_routes.py` (detail page tests).

8. **WatchedItem detail + Watch detail UI** — WatchedItem detail (`/watched-items/{id}`): add Domain row to the details grid (link → `/domains/{name}`, shown only when `domain_name` is set); show "Domain Inactive" banner when `watched_item.domain_suspended = True`. Watch detail (`/watches/{id}`): update domain link source from `watch.effective_domain` to `watch.watched_item.domain_name`; update `domain_suspended` badge source (already Watch-level, no change). Remove any remaining `watch.effective_domain` template references. Green: `tests/dashboard/test_watch_detail_inline.py`, watched-item detail tests.

## Open questions / risks

- `src/core/notifications/default_templates.py` defines `TemplateVariable("effective_domain", ...)` and `preview_fixtures.py` carries `"effective_domain": "example.com"` — these reference the *event metadata key*, not the Watch column. The metadata key stays as `"effective_domain"` (populated by `resolution.py`); only its source changes to `watch.watched_item.domain_name`. Verify no notification template rendering breaks.
- `WatchUpdate` currently exposes `effective_domain` as a directly patchable field. Removing it is a breaking API change (noted in design doc). Confirm no external scripts depend on patching `effective_domain` directly before Step 5.
- The watch-list domain filter in `context.py` currently uses `Watch.effective_domain.ilike(...)`. The equivalent via WatchedItem join is correct but should be tested for partial-match semantics (ilike on `WatchedItem.domain_name`).
- Backfill uses `LIMIT 1` on watches per WatchedItem with no stable ordering. All watches under a WatchedItem share the same InfoItem primary URL domain, so any row is correct — but add an `ORDER BY created_at` to the subquery for determinism.
- NC dashboard routes (`src/dashboard/routes.py` ~2702–2792) query `DomainNcRef.domain_name == watch.effective_domain`. These move to `watch.watched_item.domain_name`. Covered in Step 8 cleanup; flag in review if a separate step is needed.
