---
title: "#185 Phase A — Pipeline decoupling"
date: 2026-06-07
status: draft
---

# #185 Phase A — Pipeline decoupling

## Problem

Watcher calls the Archiver SDK on every pipeline cycle (`fetch_info_item_bindings`)
to resolve the URL and extraction specs for each WatchedItem. Archiver v4.0.0
removed the `role` and `parent_info_source_id` fields that Watcher's binding
partition logic depends on, breaking the pipeline at the type level. Rather than
do mechanical compat work that will be discarded when the architecture lands, we
implement Phase A of #185: store URL and specs locally on WatchedItem so the
pipeline never calls Archiver at runtime.

Phase A ships independently. #184 closes as resolved-by-architecture.

## Approach

Eight sequential steps, each leaving the test suite green. Additive schema
changes land first (step 1–2), then code is rewritten against the new shape
(steps 3–6), then old columns and tables are deleted (step 7), and finally
the dashboard is cleaned up (step 8). The Archiver test-schema fix (step 1)
is done first because integration tests will not run at all against the v4.0.0
schema without it; it is the unblocking prerequisite.

`change_revisions` starts empty after deploy. The first pipeline cycle for each
WatchedItem will see no prior fingerprint, compute one, insert a row, and
(if the content is unchanged from the operator's perspective) fire a
`CHANGE_DETECTED` notification. This one-time false-positive per WatchedItem on
the first post-deploy cycle is an accepted transition artifact — see Open
questions for the mitigation option.

## Tradeoffs / alternatives

- **Mechanical v4.0.0 compat first (#184), then redesign** — rejected because
  all compat code is deleted in Phase A; doing it first is pure throwaway work.
- **Seed `change_revisions` from `last_known_revisions`** — possible via a
  cross-schema join through `info_item_sources`, but complex and brittle. The
  one-time false-positive notifications on first cycle are preferable to a
  fragile migration. Rejected in favour of starting fresh.
- **Keep `last_known_revisions` readable during transition** — would let the
  pipeline fall back for WatchedItems without a `change_revisions` row, avoiding
  the false-positive. Adds a two-table read path that must be removed later.
  Rejected; the false-positive cost is low on a single watched-item fleet.

## Steps

1. **Archiver test schema + conftest → v4.0.0.**
   Update `tests/_information_test_models.py`: `InfoSource` drops
   `parent_info_source_id`, `schema_version`, renames `source_spec: JSONB` →
   `source_specs: JSONB[]`; `InfoItemSource` drops `role`. Update conftest
   factories: `make_info_source(url)` writes `url` + `source_specs=[...]`;
   `bind_primary_source` drops `role=None`; delete `bind_sub_aspect` (concept
   gone). Update `info_client` fixture: `_get_info_source` emits `source_specs`
   list, drops `parent_info_source_id`; `_get_info_item` binding loop drops
   `b.role`. Verify `uv run pytest -m integration` passes.

2. **Additive schema migrations (no code changes yet).**
   Single Alembic migration adds to `watched_items`:
   `effective_url TEXT NULL`, `source_specs JSONB NULL`,
   `archiver_info_source_id UUID NULL`, `last_changed_at TIMESTAMPTZ NULL`,
   `health_status VARCHAR(10) NULL DEFAULT 'unknown'`.
   Makes `info_item_id` nullable (drop `NOT NULL` + `UNIQUE`; add partial
   unique index `WHERE info_item_id IS NOT NULL`).
   Creates `change_revisions` table:
   `(id UUID PK, watched_item_id UUID FK, content_fingerprint TEXT,
   captured_at TIMESTAMPTZ, content_size_bytes BIGINT NULL,
   archiver_revision_id UUID NULL, schema_version INT)` +
   index `(watched_item_id, captured_at DESC)`.
   Creates `pending_archiver_sync` table mirroring `pending_source_revisions`
   but keyed by `change_revision_id UUID` + `watched_item_id UUID`; drops
   `info_source_id`. Verify `uv run alembic upgrade head` and full test suite
   pass (no code touches new columns yet).

3. **Data migration (backfill WatchedItem from Watches).**
   Second Alembic migration with `execute()` SQL:
   - `effective_url`: set from the `effective_url` of any active child Watch
     (subquery; leaves NULL where no Watch exists — acceptable, operators must
     supply URL on next edit).
   - `last_changed_at`: `MAX(last_changed_at)` over child Watches.
   - `health_status`: `MIN(health_status)` (error < unknown < ok) over child
     Watches, falling back to `'unknown'`.
   Verify migration runs clean on the production DB snapshot.

4. **Pipeline rewrite — `pipeline.py` and `tasks.py`.**
   Delete `src/core/watches/info_item_fetch.py`. Remove `ArchiverClient` import
   from `pipeline.py`. Remove `fetch_info_item_bindings` call from
   `check_watched_item` in `tasks.py`; read `watched_item.effective_url`
   directly. Rewrite `_process_binding` → `_extract_and_fingerprint` (takes
   `raw_content: bytes` + `source_specs: list[dict]`; tries `[0]`, falls back to
   `[1]`, `[2]` on zero-content result). Rewrite `process_watched_item`:
   single extraction path; query `change_revisions` for last fingerprint;
   on change insert `ChangeRevision`, conditionally insert
   `pending_archiver_sync`. `CHANGE_DETECTED` dispatches to all active
   non-archived child Watches; payload carries `change_revision_id` + optional
   `archiver_revision_id`. `health_status`, `last_checked_at`,
   `last_changed_at` updated on WatchedItem. Rewrite
   `tests/workers/test_pipeline.py` (mock shape changes substantially — mocks
   no longer need `source_spec` or `role`). Verify `uv run pytest
   tests/workers/test_pipeline.py` passes.

5. **Drain worker rewrite — `source_revisions_drain.py` and outbox helpers.**
   Rewrite drain to pull from `pending_archiver_sync`; load
   `watched_item.archiver_info_source_id`; call
   `client.post_source_revision(info_source_id=archiver_info_source_id, ...)`;
   back-populate `change_revisions.archiver_revision_id` on success. Delete
   `_resolve_sub_aspect_watch`; load child Watches from the WatchedItem FK
   directly. Update `src/core/sources/outbox.py` (new table name + columns).
   Update drain tests. Verify `uv run pytest tests/workers/` passes.

6. **API + schema updates.**
   Remove `target_info_source_id` from `WatchCreate` (schema + validator +
   docstrings). Update `POST /watched-items` to accept `url` + `source_specs`
   directly; `info_item_id` optional; probe `url` for `effective_url` +
   `domain_name` as before. Update Watch lifecycle routes (`PATCH`, `DELETE`,
   `/deactivate`): replace `resolve_watch_url(watch, info_client)` calls with
   `watch.watched_item.effective_url` (local join, no SDK). Add
   `GET /api/v1/watched-items/{id}/revisions` (paginated `change_revisions`
   rows). Update `tests/api/test_watches.py`, `tests/api/test_watched_items.py`.
   Verify `uv run pytest tests/api/` passes.

7. **Destructive schema cleanup.**
   Alembic migration drops from `watches`:
   `target_info_source_id`, `info_item_id`, `effective_url`,
   `last_checked_at`, `last_changed_at`, `health_status`.
   Drops `last_known_revisions` table.
   Drops `pending_source_revisions` table.
   Update ORM model classes to match. Delete `src/core/sources/revision_cache.py`
   (replaced by direct `change_revisions` query in pipeline). Verify
   `uv run alembic upgrade head` and full test suite pass.

8. **Dashboard cleanup.**
   Remove routes: `GET /info-items/search`, `GET /info-items/{id}/binding-tree`.
   Remove templates: `partials/info_item_picker/`. Remove JS:
   `info-item-picker.js`. Update WatchedItem create form (`/watched-items/new`)
   to accept URL + specs directly; remove InfoItem typeahead step. Update
   WatchedItem detail page: remove binding tree, add revision count/last
   changed from WatchedItem columns. Update `tests/dashboard/` to match.
   Verify `uv run pytest tests/dashboard/` passes and dev server smoke test
   confirms create flow works end-to-end.

## Open questions / risks

- **`effective_url` nullability:** The design says `TEXT NOT NULL`, but existing
  WatchedItems with no child Watches will have NULL after the backfill. Column
  is created nullable in step 2; the API enforces non-null on new creates.
  Tightening to NOT NULL is deferred until all production rows are confirmed
  populated (a follow-up migration).

- **First-cycle false-positive notifications:** Every WatchedItem will fire one
  spurious `CHANGE_DETECTED` on the first pipeline run after deploy (no prior
  `change_revisions` row). Acceptable on a small fleet; if undesirable, the
  mitigation is to seed `change_revisions` from `last_known_revisions` in step 3
  via a cross-schema join — user should confirm tolerance before starting.

- **`source_specs` format for non-Archiver WatchedItems:** What shape does the
  API accept in the `source_specs` field? The Archiver format is
  `[{schema_version, extraction, fingerprint}]`. For operator-created items, a
  simpler `[{extraction: {algorithm, selector}}]` may suffice. Needs a decision
  before step 6 API work begins.

- **Watch.domain_suspended column:** Currently duplicated from WatchedItem.
  Not touched in this plan — leaving as-is avoids scope creep. Should be
  removed in a follow-up.
