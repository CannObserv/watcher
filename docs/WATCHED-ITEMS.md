# Watched Items

Everything the `WatchedItem` entity owns, the surfaces that render it, and the
guards on its lifecycle. `AGENTS.md` carries the one-entity rule, the create
path, and the two invariants an agent needs on nearly every task; the detail is
here.

## Fields and schedule resolution

A `WatchedItem` owns everything: the canonical `effective_url` and `source_specs`
used by the pipeline; `default_schedule_config`, `default_tags`;
`content_media_type` (#168); `domain_name` (FK → `Domain.name`, set at create time);
`domain_suspended` (set True/False by domain deactivation/reactivation — it
gates scheduling directly, no live Domain join); `domain_default_schedule_config`
(denormalized copy of the parent Domain's cadence — the Domain tier of schedule
resolution; #205); a single optional
`TemporalProfile` (1:1, `temporal_profiles.watched_item_id`); `health_status`,
`last_checked_at`, `last_changed_at`; and its notification surface (the
item-scoped `NotificationTemplate` rows — `visibility='watched_item'`,
`watched_item_id` set; see **Notifications** below). Schedule resolution is
3-tier (#205): WatchedItem `default_schedule_config` → Domain default → system
default (`resolved_schedule_config`, `src/core/scheduling/resolution.py`).
**Display** of the resolved interval + next-check goes through one helper,
`resolve_schedule_display` (`src/core/scheduling/schedule.py`, #206): it composes the
3-tier base with the active `TemporalProfile` override (`resolve_effective_interval`)
and `compute_next_check`, returning a `ScheduleDisplay` (`interval_text`, `source`
item/domain/default, `profile_active`, `next_check`, plus a `marker` property →
`domain`/`default`/`profile`). Every surface — list (`_build_schedule_map`), detail
interval field, and the domain-detail table — renders from it, so the UI matches
`schedule_tick` even when a profile is ramping (previously the UI showed the base
cadence while the scheduler checked at the profile cadence). The profile dict shape
is `TemporalProfile.to_resolution_dict()`, shared by the scheduler and the dashboard
(`get_active_profiles_by_item` batch-loads them, mirroring `schedule_tick`). Both domain facts
(`domain_suspended`, `domain_default_schedule_config`) are denormalized onto the
WatchedItem via `ensure_domain_and_resolve_suspension` on every create/PATCH path
and back-filled across a domain's items on domain edit
(`backfill_domain_schedule_config`) — so the resolver, and the scheduler hot
path, never join Domain. Per-domain cadence is `Domain.default_schedule_config`
(a `schedule_config` interval string — operator check cadence, distinct from the
`Domain.min_interval` rate-limiter floor), editable via `PATCH
/api/v1/domains/{name}` and the domain detail page; the `reduce_frequency`
post-action throttles to 1d only when the effective cadence is faster than 1d
(never speeds a slower-than-1d item up).

## Content media type

**Content media type (#168).** `content_media_type` is the **observed** raw
`Content-Type` header (e.g. `text/html; charset=utf-8`), not an operator-declared
enum — the old `default_content_type` enum (`html`/`pdf`/`file`) was retired.
It is auto-detected by `check_watched_item`, seeded **once** from the first
successful GET response header when NULL (never auto-clobbered — refresh-on-change
is deferred to drift detection), and operator-overridable on the detail page and
via PATCH. Bounded to `CONTENT_MEDIA_TYPE_MAX_LEN` (2048) at the column, the API
schema, and the detection truncation. The **media-type essence** (lowercased
`type/subtype`, params stripped, with a URL-extension tiebreaker for
octet-stream/text-plain/absent headers) is **not stored** — it's a pure function,
`media_type.resolve_dispatch_essence(content_media_type, effective_url)`, the single
source of truth used by **both** the pipeline (`process_watched_item` picks the
extractor) **and** the API (`WatchedItemResponse.media_type_essence` is a computed
field). `ServiceRegistry.get_extractor` maps essence → extractor and is total:
`text/html`→HTML, `application/pdf`→PDF, `text/csv`/spreadsheet→CSV/Excel,
everything else (incl. `application/json`, `.xls`)→HTML fallback. A dispatched
extractor that raises on mismatched bytes is caught as `ExtractionError` and
recorded like a fetch failure (ERROR health + `CHECK_EXTRACTION_FAILED` audit +
`WATCH_ERROR`), so a mislabeled non-HTML target surfaces a signal instead of
re-firing every `schedule_tick`.

## Domain keying

**Domain keying (#197).** `WatchedItem.domain_name` == `Domain.name` == `hostname(effective_url)` — the same string by construction (all derive from one `urlparse(...).hostname` over the same `effective_url`). That equality is what lets the fetch-policy producer publish per-`Domain.name` while items carry `domain_name`, and it is why `resolve_watch_target` derives the domain with the identical helper. **One entry per hostname** — host variants (`lcb.wa.gov` vs `www.lcb.wa.gov`) are independent by design. *History:* this used to describe the in-process `DomainRateLimiter`'s bucket key; the limiter retired with the local fetch path (#241 step 5) and per-host pacing is Replicator's, but the keying invariant still holds and is still load-bearing.

## Registry linkage and lifecycle

**Every WatchedItem is an Archiver InfoItem being watched (#251).**
`archiver_info_item_id` and `archiver_info_source_id` are both **NOT NULL** —
bare-URL WatchedItems were rolled back (epic: CannObserv/archiver#137 step 1).
One create path, `POST /api/v1/watched-items`, requiring all three of
`archiver_info_item_id` + `url` + `archiver_info_source_id` (both ids validated
as canonical uppercase ULIDs at the boundary, a constraint the OpenAPI document
advertises); **no dashboard create**. The nullability had been paying for two
silent-drop branches on the SourceRevision path — both gone, so a captured
revision is always enqueued. Full detail, including why a fresh item starts
`unknown` rather than `probing`:
**[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)**. On any PATCH that sets
`effective_url` (the URL-succession path), `domain_name` is re-derived from the
URL **without** re-probing and `domain_suspended` is re-evaluated; every
create/PATCH/re-probe path (API and dashboard) shares
`ensure_domain_and_resolve_suspension` in
`src/core/domains.py` (#196). SourceRevisions are POSTed to Archiver via the
`archiver-client` SDK on every detected change; the local
`pending_archiver_sync` outbox + drain worker guarantees delivery during
Archiver outages. Notifications dispatch inline from the pipeline **once per
WatchedItem** on change detection (`notifications_dispatched ≤ 1`), with
`change_revision_id` in WatchEvent metadata. `schedule_tick` skips items that
are paused (`is_active=false`), archived, or `domain_suspended`, and applies the
temporal profile's post-actions (deactivate / archive / reduce_frequency) to the
WatchedItem itself.

## Notification visibility

**Notifications (#200).** One table — `notification_templates` — holds every
notification target. Each `NotificationTemplate` has an intrinsic `visibility`
that controls where it fires:

- `global` — every WatchedItem (`domain_name`/`watched_item_id` both NULL).
- `domain` — every WatchedItem whose `domain_name` matches.
- `watched_item` — the single `watched_item_id` only.

## Notification templates

A CHECK constraint (`ck_notification_templates_visibility_refs`) enforces that
exactly the ref column implied by `visibility` is set. There is **no separate
"configuration" object** and no junction tables — the five legacy sources
(`is_global_default` flag, `domain_nc_refs`, `watch_nc_refs`,
`watched_item_notification_templates`, `watch_notification_configs`) were
collapsed in #200. `dispatch_event_notifications` runs **one** visibility-scoped
query; **dedup is by template id** (each row fires once — one query returns each
row once), and multiple templates may target the same `remote_channel_id` with
no suppression (ratified F2). `channel_hint` is display-only; `remote_channel_id`
is the notifier-owned delivery handle — nothing dispatches off the hint.

Template mutations (create/update/delete/duplicate + their audit events) go
through one service — `src/core/notifications/templates.py` (#228) — used by
every surface below; routes stay transport adapters and own the commit.

CRUD: generic visibility-aware library at `/api/v1/notifications/templates`
(create takes `visibility` + the matching ref); item-scoped convenience at
`/api/v1/watched-items/{id}/notifications` (creates `visibility='watched_item'`),
with `GET .../effective` returning the full in-scope set (global + the item's
domain + the item) — the single answer to "which channels fire for this item".
Dashboard: the library `/notifications` create makes global templates; domain
templates are created from the domain detail page; item templates from the item
detail page. Design: [docs/plans/2026-06-19-notification-model-consolidation-design.md](../docs/plans/2026-06-19-notification-model-consolidation-design.md).

## Notification body format

**Body format — source Markdown (#224/#225).** Notification bodies are **source
Markdown**. Watcher renders no HTML: it passes the composed body to the Notifier,
which converts it per channel — CommonMark → HTML for HTML-native plugins
(Mailgun, SES, `mailto`), raw Markdown for the rest (the local Apprise path was
stripped in #137). Because CommonMark treats a lone `\n` as a *soft* break (a
space, not `<br/>`), bodies must be **block-structured**, not `\n`-joined lines —
the `change_detected` body is a Markdown **bullet list** (one fact per `<li>`;
`content._build_change_detected_body`). A `\n`-joined paragraph collapses onto
one run-on line on HTML clients (the #224 regression). Guarded by
`tests/core/notifications/test_content.py::TestMarkdownListContract`; keep it that
way when editing the composer.

## WatchEvent identity fields

**WatchEvent identity fields** are `watched_item_id`, `item_name`, `item_url`
(renamed from `watch_*` in #191). The same names are the user-facing notification
template variables; the default-template "ITEM:" link (renamed from "WATCH:" in
#221) and `change_url` point at `/watched-items/{watched_item_id}`. The
`AuditLog.watch_id` FK column was retired —
audits carry the WatchedItem as `watched_item_id` inside the JSONB `payload`
(filter via `GET /api/v1/audit?watched_item_id=<ulid>`).

## Operator surface and lifecycle guards

The Archiver service must also be installed first — see its own `docs/DEPLOYMENT.md`. Archiver authoring tools (`validate_source_spec`, `fetch_and_render`, `preview_extraction`, `propose_selectors`, `find_info_item`, atomic `create_info_item`) are documented in the Archiver repo's `AGENTS.md`.

Operators manage WatchedItem defaults (`name`, `description`, `default_schedule_config`, `default_tags`; `content_media_type` is auto-detected but overridable — #168), archive/restore lifecycle, and notification-template CRUD via the `/watched-items` dashboard. Same surface is exposed at `/api/v1/watched-items`. WatchedItems are created **only** at `POST /api/v1/watched-items`, which requires `archiver_info_item_id` + `url` + `archiver_info_source_id` (#251 — the dashboard create form is gone; Archiver provisions via "Begin Watching"). Create and PATCH accept `is_active` (#188): create defaults `true`; pass `false` to provision paused. `is_active` is the **pause/resume** toggle (distinct from archive) — paused (`is_active=false`, not archived) items are skipped by `schedule_tick` and short-circuited by the `check_watched_item` task, but stay editable. The pause/resume rules live in one place (#228): `set_watched_item_active` (`src/core/watched_items.py`) owns the guards + the `WATCHED_ITEM_PAUSED`/`RESUMED` audit events, shared by the API PATCH and the dashboard toggle. PATCH `is_active` on an archived item is rejected (409 — restore first); activation while archived is owned by archive/restore; resume (`is_active=true`) while `domain_suspended` is rejected (409 — kill-switch parity with the dashboard toggle, unified in #228). Archive stamps `archived_at` and flips `is_active` (single entity — no child cascade since #191); restore clears `archived_at` and re-activates. **Permanent delete (#210):** `DELETE /api/v1/watched-items/{id}` → 204; **archived-only** (409 `"WatchedItem must be archived before deletion"` otherwise; 404 unknown/malformed). DB-level `ON DELETE CASCADE` removes the five children (`temporal_profiles`, item `notification_templates`, `change_revisions`, `pending_archiver_sync`, `fetch_commands`); a `WATCHED_ITEM_DELETED` audit row is written **before** the delete and survives it (id lives in the JSONB payload, no FK). Local-only — Archiver-side InfoItem/SourceRevisions are untouched. Deleting the last archived item on a domain frees the #209 domain delete guard. Filter by InfoItem with `GET /api/v1/watched-items?archiver_info_item_id=<ulid>`. Trigger an immediate check with `POST /api/v1/watched-items/{id}/check-now` (202). Its pre-flight mirrors **every** short-circuit in `check_watched_item`, so a request that could not do anything is rejected instead of returning 202 over a silent no-op: 409 archived, 409 paused, 409 `domain_suspended`, 409 a fetch command already open (the #241 one-command gate — post-cutover the likeliest; the message quotes the command's age and `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS` so an operator can tell a two-second wait from a stall), 422 empty `effective_url`.

## Dashboard parity

**Dashboard parity (#190):** the dashboard surfaces pause/resume (`POST /watched-items/{id}/toggle-active` — mirrors the API 409 guards, blocks resume while `domain_suspended`, emits the `WATCHED_ITEM_PAUSED`/`RESUMED` events), check-now (`POST /watched-items/{id}/check-now` — delegates to the API route, so it inherits all five pre-flight guards; failures surface as an OOB flash carrying the API's detail text), effective_url editing (`POST /watched-items/{id}/effective-url` — re-probes to re-derive `domain_name`, leaves `source_specs` untouched), and permanent delete (`POST /watched-items/{id}/delete` — delegates to the API DELETE; the **Delete permanently** control lives in the archived branch of the detail Danger Zone only, redirects to `/watched-items` on success, surfaces the 409 as an OOB flash; #210). Pause/resume + check-now controls appear on the WatchedItem detail page and in the list rows. `source_specs` is shown read-only on detail (authoring stays in Archiver tooling). The detail page surfaces a single item-template panel (item-scoped `NotificationTemplate` CRUD) plus read-only Global/Domain inherited sections; the full API surface lives at `/api/v1/watched-items/{id}/notifications` (see **Notifications**).

## List view

**Watched Items list view** (`#172`, `#173`, `#190`): columns are Name → Last Check → Interval → Next Check → Status → Actions (per-row pause/resume toggle + check-now). The Status badge distinguishes Active / Paused / Domain Inactive / Archived. Interval and Next Check resolve through `resolve_schedule_display` (the #206 helper over the 3-tier `resolved_schedule_config`; #204, #205, #206), so an item with no explicit `default_schedule_config` shows the inherited interval with a source marker (`· domain` when inherited from the Domain cadence, `· default` from the system default, `· profile` when an active `TemporalProfile` overrides the base cadence) plus a computed Next Check rather than blanks — parity with the detail page and consistent with `schedule_tick`. Next Check is a live countdown rendered by `src/dashboard/static/js/next-check-countdown.js` (loaded globally via `base.html`; reads `data-next-check` ISO timestamp attributes, refreshes every 60 s). List has server-side name search and pagination: `GET /partials/watched-items-table?q=&page=&page_size=&include_archived=` is the HTMX partial; the full page (`GET /watched-items`) accepts the same params and SSR-includes the partial on first load. Active/All archived toggle is a segment-group that cross-includes the search input. Aspect Review column removed (#173) — too expensive per-row; will surface on WatchedItem detail page behind a Redis cache (tracked in #163).

## Domain WatchedItem counts

**Domain WatchedItem counts (#209):** the Domains-list "Watched Items" column and the domain-detail heading count *live* (non-archived) items only, and the list's "Last Checked" `max` excludes archived items too. The detail heading shows the archived remainder explicitly (`N · M archived`). The domain **delete guard** is the one place that counts the archived-inclusive total — archived WatchedItems still hold the `domain_name` FK, so deletion stays blocked while any reference exists.

## InfoItem picker removed

**InfoItem picker removed** (`#185 Phase A step 7`): the InfoItem typeahead picker (routes `GET /info-items/search`, `GET /info-items/{id}/binding-tree`; JS `info-item-picker.js`; templates `partials/info_item_picker/`) was removed. Nothing replaced it: WatchedItem-create is API-only and InfoItem-linked (#251), and no create path probes (#241) — `effective_url` comes from Archiver and `domain_name` is derived from it (the separate Watch-create flow no longer exists — #191).

## Detail page

**Watched Item detail** (`#174`, updated `#185`, `#190`, `#191`, `#199`, `#202`, `#215`): the heading carries a subdued **Watched Item** eyebrow. A single **Details** panel holds, in order, Name, the `effective_url` row (inline **Edit** — re-derives the domain without probing, #241), Domain, the Status pause/resume toggle (the sole status badge), `last_checked_at` (with the Check-now action and the **Health** badge inline — health is the result of that check, surfaced with an accessible hover/focus tooltip), `last_changed_at`, Interval (when unset, shows the inherited cadence with a `· domain`/`· default` source marker, or `· profile` when an active temporal profile overrides it; #205, #206), Content Type, Description, and Tags — all from local WatchedItem columns, no Archiver SDK calls. Below it: a read-only `source_specs` panel, then the **Notification Templates** panel (item-scoped CRUD plus read-only **Global**/**Domain** inherited sections that fire at dispatch — parity with the Domain detail page; #199, unified in #200), then **Recent Activity**. `POST /watched-items/{id}/mark-reviewed` (stamps `last_reviewed_at`) remains API-only — the dashboard route exists but is intentionally unwired; no dashboard UI until a replacement is designed.

## Recent Activity and Audit Log parity

**Recent Activity / Audit Log parity (#215).** The detail page's **Recent Activity** section and the global **Audit Log** (`/audit`) share one chip-filter partial (`partials/audit_filter_chips.html`) and one table partial (`partials/audit_table.html`), both driven by the single HTMX endpoint `GET /partials/audit-table`. The endpoint is scoped by `watched_item_id`: when present (detail page), it filters to that item, hides the redundant **Watched Item** column (`show_watched_item=False`), and targets `#wi-activity-table`; when absent (Audit Log), it shows all events with the column and targets `#audit-table`. Both surfaces paginate via `partials/pagination.html` over `get_audit_entries` / `get_audit_entries_count` (`src/dashboard/context.py`); the route helper `_audit_table_context` is the single source of the render+pager context. The pager's footer style is context-aware via a `sticky` flag (`= not item_scoped`): the item-scoped detail Recent Activity renders a flush, non-sticky footer inside the standard bordered card (parity with the sibling detail panels, dark fill `gray-800` to match; #223), while the global `/audit` keeps the viewport-anchored sticky footer (`gray-900`, matching the page). HTMX drives filtering/paging, but the page routes (`/audit` and the detail page) also honor `?event_type`/`page`/`page_size` query params, so the chip filter's no-JS Apply button and deep-links work. The event-type chips are **multi-select, OR-matched**: `event_type` is a repeatable query param (`?event_type=a&event_type=b` → `event_type IN (a, b)`), each chip change submits the whole form (`hx-include="closest form"`) so every checked chip is sent, and **Clear filter** is a full-page link (it must reset the checkboxes, which live outside the swapped table region). Pagination params are clamped to safe bounds by `clamp_pagination` (`src/dashboard/deps.py`, `PAGE_SIZES = (25, 50, 100)`) — shared by every paginated dashboard list route (watched-items, domains, audit) so a crafted `?page_size=-5`/`?page=-5` can't reach the DB as a negative/unbounded `LIMIT`/`OFFSET` (#215 CR-6). Chip choices differ by surface: the **global Audit Log derives its chips dynamically** from the event types actually present (`get_distinct_audit_event_types` → `SELECT DISTINCT event_type … ORDER BY event_type`, alphabetical/prefix-grouped), so the filter always matches the data — no dead chips, no missing chips (#217). On an unbounded `audit_log` all three per-page queries are index-backed (#218): the dominant `ORDER BY created_at DESC` list by `ix_audit_log_created_at`, and both the `event_type IN (...)` filter and this DISTINCT-chip query by the composite `ix_audit_log_event_type` `(event_type, created_at DESC)` (the WatchedItem-scoped filter keeps using the #193 `ix_audit_log_payload_watched_item_id`). The **per-item Recent Activity** uses the curated static `WATCHED_ITEM_EVENT_CHOICES` (the `check.*` + `watched_item.*` subset a single item emits). The legacy `watch.*` event prefix (retired in #191) was purged — the `EventType.WATCH_*` constants and the stray `audit_log` rows are gone (pre-production cleanup). The old friendly-summary list (`get_watched_item_activity`, `_WI_ACTIVITY_SUMMARY`) was retired — rows now show the raw event badge + a **Details** `View` action, matching the Audit Log. **Details View (#216):** the Details cell is a `View` button (or `—` for an empty payload) that expands a hidden full-width sibling `<tr>` (`colspan` = 4 with the Watched Item column / 3 without) showing the **pretty-printed** payload (`tojson(indent=2)`) read-only, styled like the Source Specs `<pre>` block, with a `Close` button. Toggling is delegated on `document` in `static/js/audit-details-toggle.js` (loaded globally via `base.html`) so it survives HTMX table swaps; the `View` button carries `aria-expanded`/`aria-controls`, and `Close` returns focus to it.

## Plans

Plans: the #191 collapse design is at [docs/plans/2026-06-16-collapse-watcheditem-watch-design.md](../docs/plans/2026-06-16-collapse-watcheditem-watch-design.md). Historical: design at [docs/plans/2026-05-15-watched-item-infoitem-first-design.md](../docs/plans/2026-05-15-watched-item-infoitem-first-design.md); #160 reshape at [docs/plans/2026-05-17-watched-item-watch-reshape.md](../docs/plans/2026-05-17-watched-item-watch-reshape.md); #161 CRUD UI at [docs/plans/2026-05-17-watched-item-crud-ui-plan.md](../docs/plans/2026-05-17-watched-item-crud-ui-plan.md). The Phase 5 cutover design ([docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md](../docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md)) is historical and was superseded by #160.
