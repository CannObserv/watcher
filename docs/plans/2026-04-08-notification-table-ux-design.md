# Notification Config Table UX Redesign

**Date:** 2026-04-08
**Issue:** TBD

## Goal

Revise the Watch detail Notifications section to align with established UI/UX patterns:

1. Move "Add notification" behind a `+ Add` toggle in the section header (was always-visible)
2. Replace the card list with a compact table (title + status columns)
3. Rename toggle button "Deactivate" → "Pause" to match the `watch_paused` event
4. Reorder action buttons: Edit · Test · Pause/Activate · Delete

## Approved Approach

**Approach A — `+ Add` in section header, inline add row via HTMX `afterbegin`.**

The section header becomes a flex row with the `Notifications` heading and a `+ Add` button. Clicking `+ Add` loads a new `notification_add_row.html` partial as the first `<tbody>` row via `hx-swap="afterbegin"`. Submit and cancel use the existing `#watch-notifications` `innerHTML` swap pattern (same as edit/toggle/delete).

## Key Decisions

- **`+ Add` button lives in `watch_detail.html`**, not the partial. This keeps the add form out of the partial's steady-state DOM, eliminating the class of HTMX target bug that caused #82.
- **Table columns: Title · Status · Actions.** Event badges and URL reveal removed from table view; still accessible via the Edit form.
- **Empty state** rendered as a single `<tr colspan="3">` row — "No notification configs yet."
- **Button label:** `"Pause"` (active) / `"Activate"` (inactive). Aligns with the `watch_paused` event name.
- **Button order:** Edit · Test · Pause/Activate · Delete. Destructive action last.

## Out of Scope

- Any changes to the Edit form content or events fieldset
- URL reveal (removed from table; no replacement needed — Edit form shows the URL)
- Notification config data model changes

## Files Changed

| File | Change |
|---|---|
| `src/dashboard/templates/pages/watch_detail.html` | Section header → flex row with `+ Add` button |
| `src/dashboard/templates/partials/watch_notifications.html` | `<ul>` → `<table>`, remove always-visible add form, button label/order |
| `src/dashboard/templates/partials/notification_add_row.html` | New — inline `<tr>` add form (colspan 3) |
| `src/dashboard/routes.py` | New route: `GET /watches/{id}/notifications/add-row` |
| `tests/dashboard/test_watch_notifications_partial.py` | Update/add tests for new structure |
