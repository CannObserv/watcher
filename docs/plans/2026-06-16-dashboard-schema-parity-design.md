# Dashboard schema parity — post #184/#185/#188/#189

**Date:** 2026-06-16
**Status:** Approved (brainstorming) — implementation plan to follow
**Scope:** admin dashboard (`src/dashboard/`) only

## Problem

The #184/#185/#188/#189 schema redesign moved Watcher to a WatchedItem-first,
URL-based model: `WatchedItem` is the canonical owner of `effective_url`,
`source_specs`, `health_status`, `last_checked_at`, `last_changed_at`, and the
new `is_active` pause/resume toggle. `Watch` dropped all per-target tracking
columns and now reads them through `watch.watched_item.*`.

The database changes did **not** fully reach the dashboard. An audit
(four parallel passes over routes + context + every Watch/WatchedItem/Domain/
Notification template, top findings verified directly) surfaced two real bugs,
five missing flagship capabilities, leftover cruft, and a set of representation
gaps.

### Audit findings (verified)

**Tier 1 — bugs (dashboard shows wrong information)**

1. Watch list & detail silently show stale "Last Checked / Last Changed".
   `watch_row.html:28,31` and `watch_detail.html:120` read `watch.last_checked_at`
   / `watch.last_changed_at` — columns dropped from `Watch` in #185 step 6.
   Jinja resolves them to silent `Undefined` → cells always render `—` / "Never".
   The list sort headers for these columns *work* (correlated subqueries in
   `get_watch_list`), so a user can sort by a column whose body is always blank.
   Correct source: `watch.watched_item.*` (relationship is `lazy="joined"`).
2. Watch-detail notification panel under-represents what fires. Dispatch unions
   **5** tiers including WatchedItem-default templates (`notify.py:192-199`);
   `_render_watch_notifications` (`routes.py:2467-2538`) queries only **4** —
   never `WatchedItemNotificationTemplate`. The core of the new inheritance
   chain is invisible on the Watch page.

**Tier 2 — missing functionality (API capability, no dashboard control)**

3. Pause/Resume WatchedItem (`is_active`, #188/#189) — badge shows state, no
   toggle. Child Watches have a toggle (`routes.py:648`); the parent doesn't.
4. Check-now (#185+) — no dashboard route or button at all.
5. Provision-paused at create (`is_active=false`) — create form has no field.
6. Edit `effective_url` (`PATCH`, #187) — not in the editable-field set.
7. `source_specs` (the pipeline-driving config) — never shown or edited.
8. mark-reviewed — dashboard POST route exists (`routes.py:1107`) but is
   UI-unreachable (intentional per AGENTS.md; flagged as a dead path).

**Tier 3 — cruft / dead code**

9. `watched_items.html:48-54` empty-state copy claims a WatchedItem is
   "auto-created on the first Watch under an Information Item" and offers a
   `New Watch` button that errors without `?watched_item_id` — obsolete post-#185.
10. `get_watch_changes()` (`context.py:192`) — Phase-5 tombstone, zero callers.
11. `snapshot_meta=None` plumbing (`routes.py:323,354`) — dropped-Snapshot tombstone.
    (InfoItem-picker removal, #185 step 7, is fully clean — no remnants.)

**Tier 4 — representation gaps & correctness risks**

12. WatchedItems list badge can't distinguish `domain_suspended` — a suspended-
    but-`is_active` item shows green "Active" (`watched_items_table.html:38-40`).
    Domain-detail WI table distinguishes correctly — inconsistent.
13. WI detail hides Health when `UNKNOWN` (`watched_item_detail.html:40`).
14. Watch detail has no Health / Last-Changed rows though parent data is loaded.
15. Domain-detail WI table omits Health and Interval/Next-Check columns.
16. Domain list "Active" filter ignores `is_active`; no "Inactive" filter option
    (`domains.html:28`, `context.py:338-342`).
17. `+ New Watch` guard ignores `is_active` — paused WI still offers it
    (`watched_item_detail.html:106`).
18. Archive button copy omits the `is_active` cascade (`watched_item_detail.html:127`).
19. WI notification-template form is a free-text `events` input — no variable
    chips / event guidance, unlike the rich NotificationTemplate form.

## Approach

Four phases mirroring the tiers. TDD throughout (Red → Green → Refactor):
render tests for template fixes, route tests including 409/422 guard paths for
new mutations, mirroring the existing dashboard test structure.

### Phase 1 — Tier 1 bugs (no new UI)

- Repoint `watch_row.html:28,31` and `watch_detail.html:120` to
  `watch.watched_item.last_checked_at` / `.last_changed_at`. Add a Last-Changed
  row + Health badge to watch detail (parent already joined).
- Extend `_render_watch_notifications` to query `WatchedItemNotificationTemplate`
  (mirror `notify.py:192-199` — active + `events.contains`), dedupe vs. existing
  tiers, render a "WatchedItem default" source group in `watch_notifications.html`.

### Phase 2 — Tier 2 features

- **Pause/resume:** new `POST /watched-items/{id}/toggle-active` mirroring
  `watch_toggle_active` — 409 on archived (restore-first), block resume while
  `domain_suspended` (kill-switch parity), emit existing
  `WATCHED_ITEM_PAUSED`/`RESUMED` events (#189), HTMX
  `watched_item_status_toggle.html` partial. Surface: **detail + list rows**.
- **Check-now:** new `POST /watched-items/{id}/check-now` mirroring API pre-flight
  (`watched_items.py:324`) — guards not-archived / not-paused / has-effective_url,
  `check_watched_item...defer_async`, `WATCHED_ITEM_CHECK_REQUESTED` audit
  (`source="dashboard"`), flash result. Button on **detail + list rows**, disabled
  when guards fail.
- **Provision-paused:** add `is_active` checkbox to `watched_item_form.html`;
  create-submit reads it (default checked).
- **effective_url edit (re-probe):** dedicated route (not inline-field) reusing
  `probe_fn` + Domain auto-create from the create-submit path → re-derives
  `effective_url` + `domain_name`, leaves `source_specs` untouched,
  `httpx.HTTPError` → flash "URL unreachable." Edit affordance on detail.
- **source_specs:** read-only formatted-JSON panel on WI detail (closes the
  invisible gap; authoring stays in Archiver tooling).
- **mark-reviewed:** remove/annotate the unreachable dashboard route to kill the
  dead path (no UI by design).

### Phase 3 — Tier 3 cruft

- Rewrite stale `watched_items.html:48-54` empty-state copy + drop the erroring
  `New Watch` button.
- Delete dead `get_watch_changes` (`context.py:192`); drop `snapshot_meta=None`
  plumbing if the template no longer reads it.

### Phase 4 — Tier 4 polish

- WatchedItems list badge: distinguish `domain_suspended` (warning badge),
  matching the domain-detail table precedence.
- WI detail: show Health even when `UNKNOWN`.
- Domain-detail WI table: add Health + Interval/Next-Check columns.
- Domain list: "Active" filter excludes `is_active=false`; add "Inactive" segment.
- `+ New Watch` guard also checks `is_active`; archive-button copy mentions the
  `is_active` cascade.
- WI notification-template form: add variable chips / event guidance for parity.

## Key decisions

- **source_specs is read-only** in the dashboard. It's a NOT-NULL JSONB array
  authored upstream via Archiver tooling; a structured editor is YAGNI. Show it,
  don't edit it.
- **effective_url editing re-probes** the URL to re-derive `domain_name` (and
  auto-create the Domain if new), reusing the create-time `probe_fn` path rather
  than a plain string set. Keeps `effective_url`/`domain_name` consistent;
  `source_specs` left untouched.
- **Pause/resume + check-now live on detail and list rows** — matches the Watch
  toggle pattern and serves operators scanning the list. Not added to the
  domain-detail WI table this round.
- **Resume is blocked while `domain_suspended`** (kill-switch parity with the
  Watch toggle); pausing/archived transitions keep the API 409 semantics.

## Out of scope

- `source_specs` editing UI (read-only only).
- mark-reviewed UI (intentional per AGENTS.md).
- `Watch.tags` UI (tags managed as WatchedItem defaults).
- Aspect/Review health column on the global WatchedItems list (deferred, #163).
