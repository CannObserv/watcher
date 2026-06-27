# Recent Activity → Audit-Log parity (shared components) — Design

**Date:** 2026-06-26
**Status:** Approved

> **Note (superseded in part):** the static `AUDIT_EVENT_CHOICES` list described
> below for the global `/audit` screen was removed in #217 — the Audit Log now
> derives its chips dynamically from the event types present in the data
> (`get_distinct_audit_event_types`). The per-item `WATCHED_ITEM_EVENT_CHOICES`
> remains as described.

## Goal

Make the Watched Item detail page's **Recent Activity** section use the same
affordances as the **Audit Log** screen (`/audit`):

- the chip-group event-type filter, and
- the `data-table` row display (Time · Event badge · Details), **minus** the
  redundant "Watched Item" column (the detail page is already scoped to one item).

Refactor so both surfaces share the table partial, the chip-filter partial, and
the backend query. Take the opportunity to fix the Audit Log's stale chip list
and add real pagination to both surfaces.

## Current state

**Audit Log** (`/audit`)
- `pages/audit_log.html`: `chip-group` (single-select via HTMX `hx-vals`) over a
  hardcoded event list. **The list is stale** — it offers `watch.created`,
  `watch.updated` (legacy events no longer emitted; lifecycle is `watched_item.*`
  since #191) and omits `check.extraction_failed`.
- `partials/audit_table.html`: `data-table` with **Time · Event · Watched Item ·
  Details (raw JSON)**.
- Backend `get_audit_entries(session, event_type, watched_item_id, limit=100,
  offset=0)`; HTMX partial route `/partials/audit-table` already accepts
  `watched_item_id`. No pagination today (flat `limit=100`).

**Recent Activity** (Watched Item detail)
- `pages/watched_item_detail.html`: a flat list (not a table), no filter, of
  `{event_type, timestamp, summary}` rows where `summary` is a friendly label.
- Backend `get_watched_item_activity(session, watched_item_id, limit=20)` +
  `_WI_ACTIVITY_SUMMARY` map (+ HTTP-status enrichment for fetch failures).

## Decisions (from interview)

1. **Detail rows match the Audit Log exactly** — event badge + raw JSON
   `Details`. The friendly-summary path (`get_watched_item_activity`,
   `_WI_ACTIVITY_SUMMARY`) is **retired**.
2. **Refactor + fix stale chips** — extract shared components AND correct the
   Audit Log chip list (drop `watch.*`, use `watched_item.*`, add
   `check.extraction_failed`).
3. **Detail chips = item-relevant subset** — `check.*` + `watched_item.*`
   (excludes domain-level events that never carry a `watched_item_id`).
4. **Real pagination on both** surfaces, reusing `partials/pagination.html`.

## Approach

### Shared table partial

`partials/audit_table.html` gains a `show_watched_item` flag (default `True`).
When `False`, the Watched Item `<th>` and `<td>` are omitted (and the empty-state
`colspan` adjusts). The detail page includes it with `show_watched_item=False`.

### Shared chip-filter partial

New `partials/audit_filter_chips.html`, parameterized:
- `event_choices` — list of `(value, label)` tuples,
- `selected_event_type` — for the checked state,
- `chips_target` — hx-target id (`#audit-table` vs `#wi-activity-table`),
- `chips_watched_item_id` — optional; when set, rendered as a hidden
  `<input name="watched_item_id">` and merged into the chips' `hx-vals` so the
  filter stays scoped to the item.

Two choice constants in `src/dashboard/context.py`:
- `AUDIT_EVENT_CHOICES` — corrected full list for `/audit`.
- `WATCHED_ITEM_EVENT_CHOICES` — the item-relevant subset.

### One partial endpoint, two surfaces

`/partials/audit-table` already takes `watched_item_id` + `event_type`; extend it
with `page` / `page_size`. The route derives, from whether `watched_item_id` is
present:
- `show_watched_item = watched_item_id is None`,
- the pagination wiring (`base_url=/partials/audit-table`, `hx_target`,
  `extra_params` carrying `event_type` and — for the item view — `watched_item_id`).

This keeps a single source of truth for the table; the detail page is just the
`watched_item_id`-scoped, column-hidden projection of it.

### Backend

- Add `get_audit_entries_count(session, event_type, watched_item_id)` mirroring
  `get_watched_items_total_count`, for pager totals.
- `get_audit_entries` already supports `event_type` / `watched_item_id` /
  `limit` / `offset` — used directly with `offset = (page-1) * page_size`.
- Delete `get_watched_item_activity` and `_WI_ACTIVITY_SUMMARY`.

### Routes

- `/audit` page — accept `page` / `page_size` / `event_type`; pass
  `AUDIT_EVENT_CHOICES` + count for the pager. SSR-include the table partial.
- `/partials/audit-table` — accept `page` / `page_size`; serve both surfaces.
- `watched_item_detail_page` — drop `activity`; fetch the first page of entries +
  count, pass `WATCHED_ITEM_EVENT_CHOICES`, `show_watched_item=False`, and the
  `#wi-activity-table` target. The detail template SSR-includes the shared chip +
  table partials (mirrors how `/watched-items` SSR-includes its table partial).

### Pagination wiring detail

`pagination.html` expects `page, page_size, total_count, base_url, extra_params,
hx_target, hx_include`. Pass `hx_include` covering the filter fields so a
page-size change preserves them:
- Audit: `[name='event_type']`.
- Detail: `[name='event_type'],[name='watched_item_id']` (the hidden input).

## Testing (TDD)

- `audit_table.html` omits the Watched Item column when `show_watched_item=False`;
  includes it otherwise.
- Detail page activity section renders `chip-group` + `data-table` + pagination;
  no friendly-summary list; Watched Item column absent.
- `/partials/audit-table?watched_item_id=…` scopes results, hides the column,
  paginates.
- Audit Log chips contain `watched_item.created` and **not** `watch.created`.
- `get_audit_entries_count` returns correct totals under filters.
- Update `test_detail_shows_check_activity` — currently asserts the friendly
  "Checked — no change"; will assert the `check.no_change` event badge instead.
- Existing audit-log tests still pass.

## Out of scope

- ~~Multi-select event filtering backend (chips remain single-select as today).~~
  **Superseded:** single-select shipped with a desync bug (checkbox chips behaved
  as single-select replace); the #215 follow-up reworked it to true multi-select
  with OR semantics. See AGENTS.md "Recent Activity / Audit Log parity".
- Per-item "mark reviewed" dashboard UI (still intentionally unwired).
- Audit payload shape changes.
- Filter/sort persistence across navigations (localStorage).
