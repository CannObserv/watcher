---
title: Dashboard schema parity — implementation steps
date: 2026-06-16
status: draft
---

# Dashboard schema parity — implementation steps

## Problem

The #184/#185/#188/#189 schema redesign (WatchedItem-first, URL-based) did not
fully reach the admin dashboard: two display bugs, five missing flagship
controls, cruft, and representation gaps. Full audit and rationale in the design
doc: `docs/plans/2026-06-16-dashboard-schema-parity-design.md`. This plan is the
execution checklist.

## Approach

Four phases, executed in order, TDD throughout (Red → Green → Refactor). Phase 1
fixes correctness bugs (no new UI) and is independently shippable. Phase 2 adds
the new operator controls. Phases 3–4 are cleanup and polish. Each phase ends
green (`uv run pytest`, `uv run ruff check .`) and is committed separately so the
high-value bug fixes can land without waiting on the polish.

## Tradeoffs / alternatives

- **One big commit** — rejected; the Tier 1 bugs are higher urgency than Tier 4
  polish and should be reviewable/shippable on their own.
- **Skip the plan, work from the design doc** — rejected; the design doc captures
  *what/why* and decisions, not the ordered, independently-verifiable steps and
  TDD checkpoints. Multi-file dashboard work across routes + templates + tests
  warrants a checklist.
- **source_specs raw-JSON editor / effective_url plain set** — rejected during
  brainstorming (see design doc Key Decisions): read-only source_specs;
  effective_url edits re-probe.

## Steps

### Phase 1 — Tier 1 bugs (ship first)

1. **Watch timestamps.** Failing render test asserting watch list row + detail
   show the parent's `last_checked_at`/`last_changed_at`. Repoint
   `watch_row.html:28,31` and `watch_detail.html:120` to `watch.watched_item.*`.
   Add Last-Changed row + Health badge to `watch_detail.html` from
   `watch.watched_item.*`. Green + commit.
2. **Notification tier.** Failing test: a Watch whose parent WatchedItem has an
   active `WatchedItemNotificationTemplate` matching the event shows that tier on
   the detail notification panel. Extend `_render_watch_notifications`
   (`routes.py:2467`) to query `WatchedItemNotificationTemplate` (active +
   `events.contains`), dedupe vs. global/domain/watch-assigned, render a
   "WatchedItem default" group in `watch_notifications.html`. Green + commit.

### Phase 2 — Tier 2 features

3. **Pause/resume.** Failing route tests: toggle flips `is_active` + emits
   `WATCHED_ITEM_PAUSED`/`RESUMED`; 409 on archived; 409 resume while
   `domain_suspended`. Add `POST /watched-items/{id}/toggle-active` +
   `partials/watched_item_status_toggle.html`; wire toggle into WI detail and
   `watched_items_table.html` rows. Green + commit.
4. **Check-now.** Failing route tests mirroring API pre-flight (409
   archived/paused, 422 empty effective_url, 202/defer on success). Add
   `POST /watched-items/{id}/check-now` (`defer_async` +
   `WATCHED_ITEM_CHECK_REQUESTED` audit `source="dashboard"`, flash). Button on
   WI detail + list rows, disabled when guards fail. Green + commit.
5. **Provision-paused.** Failing test: create form `is_active=false` provisions a
   paused WatchedItem. Add `is_active` checkbox (default checked) to
   `watched_item_form.html`; read it in `watched_item_create_submit`. Green.
6. **effective_url re-probe edit.** Failing tests: edit re-probes → updates
   `effective_url` + `domain_name`, auto-creates Domain if new, leaves
   `source_specs` untouched; `httpx.HTTPError` → flash. Dedicated route reusing
   `probe_fn` + Domain-create logic from `watched_item_create_submit`; edit
   affordance on WI detail. Green + commit.
7. **source_specs display + mark-reviewed cleanup.** Add read-only formatted-JSON
   `source_specs` panel to `watched_item_detail.html` (test: renders specs,
   handles empty). Remove/annotate the UI-unreachable mark-reviewed dashboard
   route. Green + commit.

### Phase 3 — Tier 3 cruft

8. Rewrite `watched_items.html:48-54` empty-state copy; drop the erroring
   `New Watch` button (test: empty state renders, no `/watches/new` link without
   `watched_item_id`). Delete dead `get_watch_changes` (`context.py:192`); drop
   `snapshot_meta=None` plumbing if `watch_detail.html` no longer reads it.
   `ruff check` clean. Green + commit.

### Phase 4 — Tier 4 polish

9. **Status/health representation.** WatchedItems list badge distinguishes
   `domain_suspended` (warning); WI detail shows Health even when `UNKNOWN`;
   domain-detail WI table gains Health + Interval/Next-Check columns. Render
   tests per change. Green.
10. **Domain filter + guards + copy.** Domain list "Active" filter excludes
    `is_active=false` + add "Inactive" segment (`context.py:338-342`,
    `domains.html:28`); `+ New Watch` guard checks `is_active`; archive-button
    copy notes the `is_active` cascade. Tests for the filter change. Green + commit.
11. **WI notification-template form parity.** Add variable chips / event guidance
    to `watched_item_template_form.html` matching the NotificationTemplate form.
    Green + commit.

### Close-out

12. Full suite + lint green; if any DB/CSS touched, run the documented build/restart
    steps. Update AGENTS.md "Watches" section if operator-facing dashboard behavior
    changed (pause/resume, check-now, effective_url edit now in dashboard).

## Open questions / risks

- **check-now task import in the dashboard layer.** Confirm
  `check_watched_item.configure().defer_async` is callable from the dashboard
  route context (Redis/procrastinate configured) as it is from the API; if not,
  the dashboard route should delegate to the same enqueue helper rather than
  duplicate it.
- **effective_url re-probe + existing child Watches.** Re-deriving `domain_name`
  moves the WatchedItem to a different Domain; child Watches inherit via the
  parent, so no Watch rows change, but `domain_suspended` state is per-WatchedItem
  and is not re-evaluated on re-probe. Acceptable (operator action), noted.
- **Phase ordering for review.** Phase 1 is intended to ship independently; confirm
  the reviewer wants separate PRs/commits per phase vs. one branch.
