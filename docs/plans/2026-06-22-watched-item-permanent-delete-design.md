# Design: Permanent delete for archived WatchedItems

**Date:** 2026-06-22
**Issue:** #210

## Goal

Add a permanent (hard) delete affordance for archived `WatchedItem`s, in both
the API and the dashboard Danger Zone. Motivated by data cleanup, GDPR-style
removal, and removing test items.

## Background — why the affordance is missing

A hard delete existed for the old `Watch` entity: `DELETE /api/watches/{watch_id}`
(#8, [docs/plans/2026-03-22-delete-watch-endpoint.md](2026-03-22-delete-watch-endpoint.md)),
surfaced in the detail Danger Zone as "Delete permanently (if archived)"
([docs/plans/2026-03-27-watch-detail-inline-edit-design.md](2026-03-27-watch-detail-inline-edit-design.md)).
When #191 collapsed `Watch` into `WatchedItem`, the `/watches*` routes were
retired wholesale
([docs/plans/2026-06-16-collapse-watcheditem-watch-design.md](2026-06-16-collapse-watcheditem-watch-design.md))
and an equivalent `WatchedItem` delete was never re-added. The gap is an
artifact of the collapse, **not** a policy against permanent deletion. Today the
Danger Zone offers only Archive/Restore.

## Approved approach

### API contract

`DELETE /api/v1/watched-items/{watched_item_id}` → **204 No Content**

Guards, in order:

1. **404** — id not found / unparseable ULID (via `_get_or_404`, which uses
   `parse_ulid`).
2. **409** — `archived_at IS NULL` (item not archived). Detail:
   `"WatchedItem must be archived before deletion"`. This is the only state
   guard; archived already implies `is_active=False`.
3. On success: write the audit row **before** the delete, then
   `session.delete(wi)`, `commit`, return 204.

### Data handling — DB-level cascade, no app-level fan-out

A single `session.delete(wi)`; Postgres cascades the four FK children, all
already declared `ondelete="CASCADE"`:

| Table | FK column |
|---|---|
| `temporal_profiles` | `watched_item_id` |
| `notification_templates` (item-scoped only) | `watched_item_id` |
| `pending_archiver_sync` | `watched_item_id` |
| `change_revision` | `watched_item_id` |

This is the same "DB-level cascade over application-level" decision #8 made.
Integration tests build the schema with `Base.metadata.create_all`, which
reproduces these column-level FK cascades — **no conftest trigger work** (this
is not a trigger).

Side effects:

- **Domain delete guard frees up.** The deleted item no longer holds
  `domain_name`, so a domain blocked *solely* by this archived item becomes
  deletable — consistent with #209's archived-inclusive delete guard.
- **Archiver untouched.** The `archiver_info_item_id` link is dropped locally;
  the InfoItem / SourceRevisions remain in Archiver, the system of record for
  content identity. No SDK call (local-only delete).
- **`pending_archiver_sync` rows cascade away.** Any undelivered outbox entries
  for this item are discarded — acceptable, since the item is being permanently
  removed and there is nothing left to sync.

### Audit + event type

Add `WATCHED_ITEM_DELETED = "watched_item.deleted"` to the `EventType` enum. The
stale `WATCH_DELETED = "watch.deleted"` from #8 is left untouched (removing it is
out of scope). Audit payload carries `watched_item_id`, `name`, and `url`; the
trail survives the row deletion because `AuditLog` has no FK to `watched_items`
(the id lives in the JSONB payload). `source="api"` / `source="dashboard"`.

### Dashboard

- **Route:** `POST /watched-items/{id}/delete` (HTML forms cannot issue DELETE;
  matches the existing `/archive` and `/restore` POST idiom). Delegates to the
  shared API delete function, then:
  - HTMX → `HX-Redirect: /watched-items` (the detail page no longer exists).
  - non-HTMX → `RedirectResponse("/watched-items", 303)`.
  - A 409 (somehow un-archived) surfaces as an OOB error flash, mirroring
    check-now's guard handling.
- **UI:** in the **archived** branch of the Danger Zone
  ([src/dashboard/templates/pages/watched_item_detail.html](../../src/dashboard/templates/pages/watched_item_detail.html)),
  add a second block below Restore:

  > **Delete this Watched Item permanently** — Removes the item and all its
  > history (revisions, profile, item notifications). Cannot be undone.
  > → `[ Delete permanently ]` (`.btn btn-danger`)

  `hx-post="/watched-items/{id}/delete"`,
  `hx-confirm="Permanently delete {{ name }}? This cannot be undone."` — the
  exact domain-delete pattern. Restore stays the primary recovery path; delete
  is the deliberate second step. The block appears **only when archived**, so the
  live-item branch of the Danger Zone is unchanged.

## Key decisions

- **Archived-only precondition** (vs. inactive-or-archived, vs. any state) —
  matches the original "Delete permanently (if archived)" design and forces the
  archive → delete two-step, so a live item can never be deleted in one action.
- **Local-only delete** (vs. also notifying Archiver) — Archiver is the system of
  record for content identity; no cross-service coupling, no new Archiver
  endpoint, no cross-repo dependency.
- **Simple `hx-confirm`** (vs. type-name-to-confirm modal) — consistent with the
  existing domain-delete idiom; archived-only + confirm is sufficient friction.
- **DB-level cascade** (vs. app-level deletes) — safer, simpler, already wired
  via existing FK constraints.
- **Audit before delete** — the JSONB-payload `AuditLog` row preserves the trail
  after the row is gone.

## Testing (TDD, red first)

- **API:** delete archived → 204; row gone; each child table empty for that id;
  audit row written and survives. Delete non-archived → 409. Delete unknown /
  malformed id → 404. Delete frees a domain-delete guard (integration).
- **Dashboard:** archived detail renders the Delete block; non-archived detail
  does **not**; `POST /delete` (HTMX) → `HX-Redirect`, (non-HTMX) → 303 to
  `/watched-items`; row actually deleted.

## Out of scope

- Bulk delete.
- Retention policies / scheduled cleanup.
- Archiver-side deletion of the linked InfoItem.
- Removing the legacy `WATCH_DELETED` enum value.
- A list-row delete action (detail-page only).
- Type-name-to-confirm modal.
