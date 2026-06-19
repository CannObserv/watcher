---
title: Consolidate the notification template/config model onto a single scoped table (#200)
date: 2026-06-19
status: draft
---

# Notification model consolidation (post-#191)

## Problem

The notification surface accreted across #137/#160/#191/#198 and now carries
structural debt (audit in #200, confirmed against the code):

- **F1** — `watched_item_notification_templates` and `watch_notification_configs`
  have byte-identical columns, both keyed `watched_item_id`, both dispatched,
  never deduped against each other. Their only historical difference
  (WatchedItem-level vs Watch-level) evaporated when #191 collapsed `Watch`.
- **F2** — dispatch dedups sources 1–3 by `template_id` and 4–5 not at all;
  nothing keys on the channel.
- **F3** — stale `Watch*` naming and actively-wrong docstrings
  (*"inherited by Watches"*, *"this watch's parent WatchedItem"*, *"Approach B"*).
- **F4** — a channel attaches to an item either as a *ref* to a library
  template (`watch_nc_refs`) or as an *inline copy*
  (`watched_item_notification_templates`) — two lifecycles, two UIs. In practice
  the "library" is barely reused: domain templates are minted 1:1 with their
  junction, globals are a flag, and item-refs have no dashboard UI at all.
- **F5** — the WatchedItem detail page surfaces only the two inline tables (plus
  read-only globals/domain since #199); `watch_nc_refs` is invisible. "Which
  channels fire for this item" is not answerable from one place.
- **F6** — `channel_hint` is display-only; `remote_channel_id` is the real
  target. Undocumented.

Five dispatch sources across four tables plus a flag, with inconsistent dedup
and legacy naming, is the wrong base to add features on.

## Approach

**Design A — one scoped table.** Collapse all five dispatch sources into the
single `notification_templates` table by giving each template an intrinsic
**`visibility`** that controls where it fires (matches the issue's stated
preference). The user-facing noun is just **"Notification Template"** — there is
no separate "Configuration" object; the scope and channel are columns on the
template row.

- Add `visibility` (`'global' | 'domain' | 'watched_item'`), `domain_name`
  (FK→`domains.name`, nullable), `watched_item_id` (FK→`watched_items.id`,
  nullable). Drop `is_global_default`.
- A CHECK constraint enforces visibility/ref consistency: `global` → both refs
  NULL; `domain` → `domain_name` set, `watched_item_id` NULL; `watched_item` →
  `watched_item_id` set, `domain_name` NULL.
- Drop `domain_nc_refs`, `watch_nc_refs`, `watched_item_notification_templates`,
  `watch_notification_configs` — and the `WatchNcRef`, `DomainNcRef`,
  `WatchNotificationConfig`, `WatchedItemNotificationTemplate` model classes.
- `remote_channel_id` and `channel_hint` stay on the row (channel binding is
  a property of the scoped template, not a separate object).

(`visibility`, not `scope` — `content_config` already uses `scope` for
content-block applicability; the two are different axes and the distinct name
keeps them apart.)

Dispatch becomes a single query, with id-dedup automatic (one row = one
candidate; a row matches the item by exactly one visibility path so no row can
appear twice):

```sql
WHERE is_active AND events @> ARRAY[:event] AND (
      visibility = 'global'
   OR (visibility = 'domain'        AND domain_name = :domain)
   OR (visibility = 'watched_item'  AND watched_item_id = :item))
```

Dedup rule, documented (ratifies F2): **one notification fires per matching
template row; multiple templates may target the same `remote_channel_id` and
all fire — no channel-level suppression.** Physically rename to drop `Watch*`
(table renames folded into the same migration) and rewrite the misleading
docstrings (F3). The dispatch query, surfaced read-only on the WatchedItem
detail page and at one API endpoint, becomes the single answer to F5.

## Tradeoffs / alternatives

- **Design B — `NotificationTemplate` (content) + `NotificationConfiguration`
  (scope binding), library reuse preserved** — rejected because the library
  indirection is barely used today (domain templates are 1:1 with their
  junction; globals are a flag; item-refs have no UI), so a second table +
  junction-resolution buys reuse that isn't happening. Revisit only if a
  concrete need arises to attach one identical template to many domains/items;
  the scoped-row model can be widened to a junction later without data loss.
- **Rename + docstring sweep only, leave the five sources** — rejected: fixes
  F3 but leaves F1/F2/F4/F5, i.e. the structural debt #200 was opened to remove.
- **Status quo** — rejected: #199 already paid for read-only surfacing because
  the model couldn't answer "what fires for this item" cleanly; the next feature
  pays again.

## Steps

1. **Migration — widen + backfill + drop, reversible.** One Alembic revision:
   add `visibility`/`domain_name`/`watched_item_id` + the CHECK constraint to
   `notification_templates`; backfill (globals→`visibility='global'`; each
   `domain_nc_refs` row→`visibility='domain'`; each `watch_nc_refs` row→
   `visibility='watched_item'`; copy every `watched_item_notification_templates`
   and `watch_notification_configs` row into a new `visibility='watched_item'`
   template); **drop orphan library templates** — rows that are neither global
   nor referenced by any junction, logging the dropped count first (they fire
   nowhere today); drop `is_global_default` and the four tables. A library
   template attached at N scopes is expanded to N rows (first attachment updates
   in place, each extra INSERTs a clone). Downgrade reverses: recreate the four
   tables and re-split rows by visibility (orphan drop is not reversed — it was a
   no-op on dispatch). **Pre-flight queries to run and record in the revision
   docstring:** templates with >1 attachment (need cloning) and the orphan count.
   Mirror the schema into [tests/conftest.py](tests/conftest.py)'s `create_all`
   (no triggers involved). *Verifiable:* `alembic upgrade head` then
   `downgrade -1` round-trips on a copy of prod data; row counts before/after
   reconcile (modulo the logged orphan drop).
2. **Models.** Add `visibility`/`domain_name`/`watched_item_id` to
   [notification_template.py](src/core/models/notification_template.py); delete
   `WatchNcRef`, `DomainNcRef`, `WatchNotificationConfig`,
   `WatchedItemNotificationTemplate` and their files; rewrite the
   `NotificationTemplate` docstring to the post-#191 world. Update
   [models/__init__.py](src/core/models/__init__.py). *Verifiable:* imports
   resolve; `ruff check` clean.
3. **Dispatch.** Rewrite `dispatch_event_notifications`
   ([notify.py](src/core/notifications/notify.py)) to the single scoped query;
   collapse `DispatchCandidate.source` to the `visibility` value; document the dedup
   rule verbatim in the docstring. *Verifiable:* dispatch tests green; an item
   with global+domain+item templates fires once per row, and two templates on
   one channel both fire (assert against the existing behavior).
4. **API.** Fold item notifications into the template CRUD: a scoped-create
   endpoint replaces the `assign/unassign` junction routes
   ([notification_templates.py](src/api/routes/notification_templates.py)) and
   the per-item config CRUD
   ([notification_configs.py](src/api/routes/notification_configs.py)); update
   the schemas ([notification_template.py](src/api/schemas/notification_template.py),
   [notification_config.py](src/api/schemas/notification_config.py)) — `visibility`
   in, ref-count fields out. Add `GET /watched-items/{id}/notifications/effective`
   returning the full dispatch set (the F5 single surface). *Verifiable:* route
   tests green; OpenAPI reflects the new shape.
5. **Dashboard.** Replace the inline-template + config panels with one
   visibility-grouped "Channels that fire for this item" view backed by the
   effective query; reuse one create/edit form across scopes (the UI-reuse
   goal). Update
   [context.py](src/dashboard/context.py) and
   [routes.py](src/dashboard/routes.py); remove the dead inline-template routes
   (`routes.py:948-1042`). *Verifiable:* detail page renders all scopes; domain
   and global create flows still work.
6. **Audit + docs.** Retire/replace `WATCH_NC_ASSIGNED`/`WATCH_NC_UNASSIGNED`
   and unify item notification audit events; add the F6 one-liner asserting
   `channel_hint` is display-only and never routes. Update `AGENTS.md`'s
   notification description and link this doc. *Verifiable:* `rg "WatchNcRef|
   WatchNotificationConfig|WatchedItemNotificationTemplate|is_global_default|
   Approach B|child Watch"` over `src/` returns nothing.

## Decisions (resolved 2026-06-19)

- **Orphan library templates** — **drop**, logging the count first (they fire
  nowhere today; the drop is a dispatch no-op and is not reversed on downgrade).
- **Audit-event continuity** — **rename go-ahead.** Retire `WATCH_NC_*` and the
  separate `NOTIFICATION_CONFIG_*` events; new code emits the unified
  `NOTIFICATION_TEMPLATE_*` set. Historical `audit_log` rows keep their old enum
  string values (history is not rewritten).
- **Column name** — **`visibility`** (not `scope`), to avoid collision with
  `content_config.scope` (content-block applicability in
  [content.py](src/core/notifications/content.py)).
- **Vocabulary** — single noun **"Notification Template"**. No separate
  "Configuration" object; scope and channel are columns on the template row.

## Open questions / risks

- **Migration data shape.** The expand-on-multi-attachment logic assumes few/no
  shared templates; the pre-flight count (step 1) confirms. Cloning a widely
  shared template inflates row count — accepted; log it. No blocker.
