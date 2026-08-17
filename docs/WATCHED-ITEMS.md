# Watched Items

Everything the `WatchedItem` entity owns: fields, schedule resolution, registry
reconciliation, and notifications. The operator surface that renders it and the
guards on its lifecycle are in
[WATCHED-ITEMS-DASHBOARD.md](WATCHED-ITEMS-DASHBOARD.md). `AGENTS.md` carries
the one-entity rule, the create path, and the handful of invariants an agent
needs on nearly every task; the detail is here.

## Fields and schedule resolution

A `WatchedItem` owns everything: the canonical `effective_url` and `source_specs`
used by the pipeline; `default_schedule_config`, `default_tags`;
`content_media_type` (#168); `domain_name` (FK → `Domain.name`, set at create time);
`domain_suspended` (set True/False by domain deactivation/reactivation — it
gates scheduling directly, no live Domain join); `domain_default_schedule_config`
(denormalized copy of the parent Domain's cadence — the Domain tier of schedule
resolution; #205); a single optional
`TemporalProfile` (1:1, `temporal_profiles.watched_item_id`); `health_status`,
`last_checked_at`, `last_changed_at`, `last_observed_at` (#264 — advances only
on successful extraction, changed or unchanged alike; `last_checked_at` advances
on *every* outcome because it is the anti-thrash scheduling stamp (#168), so the
pair distinguishes "content verified current as of T" from "we tried at T" —
next-due derives from the latter, never the former); and its notification surface (the
item-scoped `NotificationTemplate` rows — `visibility='watched_item'`,
`watched_item_id` set; see **Notifications** below). Schedule resolution is
4-tier under a floor (#205, #254): `announced_schedule_config` → WatchedItem
`default_schedule_config` → Domain default → system default, then
`max(resolved, throttle_floor_interval)` (`resolved_schedule_config`,
`src/core/scheduling/resolution.py`).
**Display** of the resolved interval + next-check goes through one helper,
`resolve_schedule_display` (`src/core/scheduling/schedule.py`, #206): it composes the
3-tier base with the active `TemporalProfile` override (`resolve_effective_interval`)
and `compute_next_check`, returning a `ScheduleDisplay` (`interval_text`, `source`
registry/item/domain/default, `profile_active`, `throttled`, `next_check`, plus a
`marker` property → `profile`/`throttled`/`registry`/`domain`/`default`, in that
precedence — whichever is actually in force). Every surface — list (`_build_schedule_map`), detail
interval field, and the domain-detail table — renders from it, so the UI matches
`schedule_tick` even when a profile is ramping (previously the UI showed the base
cadence while the scheduler checked at the profile cadence). The profile dict shape
is `TemporalProfile.to_resolution_dict()`, shared by the scheduler and the dashboard
(`get_active_profiles_by_item` batch-loads them, mirroring `schedule_tick`). Both domain facts
(`domain_suspended`, `domain_default_schedule_config`) are denormalized onto the
WatchedItem via `ensure_domain_and_resolve_suspension` on every create/PATCH path
and back-filled across a domain's items on domain edit
(`backfill_domain_schedule_config`) — so the resolver, and the scheduler hot
path, never join Domain. Cadence is validated at the API write boundary by the same helper as the Domain
boundary (`validate_optional_schedule_config`, #205): a non-`None` config must carry a
parseable `interval`, and `{}` is rejected — delegation has exactly one spelling,
`None`/omit (the direction cannobserv#324 settled for the registry document). The rule
is held at the boundary because `schedule_tick` resolves every item in one task — an
unparseable stored interval raises out of `compute_next_check` and stops scheduling
for the whole system, not just its own row. The resolver's `{}`-passes-through branch
survives as defensive rendering for legacy rows.

Per-domain cadence is `Domain.default_schedule_config`
(a `schedule_config` interval string — operator check cadence, distinct from the
`Domain.min_interval` rate-limiter floor), editable via `PATCH
/api/v1/domains/{name}` and the domain detail page; the `reduce_frequency`
post-action throttles to 1d only when the effective cadence is faster than 1d
(never speeds a slower-than-1d item up).

## Registry reconciliation (#254)

`info.registry` announcements are the authority on cadence and active state.
`src/workers/registry_reconcile.py` makes `watched_items` match them; the stream
mechanics (groupless tail, replay from `0-0`, no DLQ, `generation` ordering) are in
[ARCHITECTURE.md](ARCHITECTURE.md) → *Redis and the bus*.

**What an announcement owns**, and nothing else: `archiver_info_source_id`,
`effective_url`, `source_specs`, `announced_schedule_config`, `is_active` — plus
`domain_name` and its two denormalized facts, and **only when the host actually
moves**. Re-deriving the domain on every announcement would clear a
`domain_suspended` an operator set, which is host-level mechanism the registry has
no opinion on.

**What survives reconciliation**: `health_status`, `last_checked_at`,
`last_observed_at`, `last_changed_at`, `last_reviewed_at`, `domain_suspended`, `archived_at`,
`throttle_floor_interval`, `default_schedule_config`, `content_media_type`,
`default_tags`, `description`, `name`, notification config, audit rows, fetch-command
history. Pinned by `TestLocalColumnsSurvive` — "we did not write it" is a weaker
guarantee than "a test fails if someone does".

**Three signals, and a fourth that is not a signal.** `revoked: true` deletes the
row (and records the generation in `revoked_info_items`, so a stale live
announcement arriving after the tombstone cannot resurrect it). `active: false`
keeps the row and stops scheduling — collapsing that into revoked loses the pause on
the next reconcile. `active: true` schedules. `active: null` is an **abstention**:
the registry has no opinion yet, so the column is left exactly as it is. Reading
`null` as `true` would un-pause every item an operator paused, which is precisely
what the rollout window looks like before CannObserv/archiver#150's import populates
the column.

**A local pause is not sticky — and since the 2026-08-13 cutover, not offered.**
`active` applies unconditionally, and once an item is reconciled
(`applied_generation` set) the API PATCH and the dashboard toggle both 409/flash
naming Archiver as the authority (`RegistryOwnedActivationError` in
`set_watched_item_active`) — a control that silently reverts within the snapshot
period is worse than a refusal that says where the control lives. Never-announced
rows keep the local toggle. Item-level pause lives in Archiver's dashboard alone.

**The guard covers all five owned columns, because the snapshot cannot repair local
drift.** The hourly republish carries the same generation, which the `>` ordering
guard ignores as stale — so a local write to an announcement-owned column diverges
until the next *real* registry mutation, not the next snapshot. Hence: PATCH 409s
`effective_url` / `source_specs` / `archiver_info_source_id` on reconciled items
(the dashboard URL edit flashes the same rule), and **restore clears `archived_at`
without re-activating** a reconciled item — archive→restore was otherwise a
two-step bypass of the pause guard. A restored registry-owned item stays paused
until Archiver re-arms it — one click, not a round-trip: Archiver's watch-active
route writes and announces unconditionally, so pressing resume there propagates
even when it already considers the item active. Watcher-local fields (name, description, tags, item
cadence, media type) stay editable everywhere. What remains legitimately Watcher's is
*mechanism* — local backoff, `domain_suspended` as the host-level break-glass, and
the throttle floor. `archived_at` is never touched, so an `active: true` against an
archived row reconciles the row's contents but no-ops on scheduling (`schedule_tick`
gates on `archived_at IS NULL` too) rather than resurrecting it.

**Two cadence absences, one answer.** `watch_spec` is required on a live
announcement since cannobserv#324, so delegation is spelled exactly one way:
`{"schema_version": 1}` with no `interval`, meaning *apply your own default* — for
this repo the per-domain tier. An `interval` that does not parse resolves the same
way and **must not stop scheduling**; co-core deliberately does not validate the
document's contents, because raising at decode on a no-DLQ stream would drop the
message and leave the key stale.

**The announced cadence does not live in `default_schedule_config`.** That column
has an operator writing to it, so reconciling into it would let the hourly snapshot
revert every operator edit — and it is what archiver#150 imports out of Watcher. The
`reduce_frequency` throttle moved to a floor for the same reason in the other
direction: as a tier it would be outranked by the announced cadence and silently
cleared on the next announcement.

**The floor is releasable, and only by an operator.** Writing an explicit item
cadence — `PATCH /api/v1/watched-items/{id}` with `default_schedule_config`, or the
dashboard's inline interval field — clears `throttle_floor_interval`. Both go through
`set_item_schedule_config` (`src/core/watched_items.py`), the single owner of that
write; `set_item_schedule_interval` is the string-shaped front door the dashboard
uses. Without that the escape hatch would be gone: before the
split, editing the interval *was* how a throttle was undone, and a floor nothing
clears means one temporal profile firing caps an item at 1d forever while the
operator's edits appear to do nothing. Reconciliation deliberately does not clear it;
the registry has no opinion on mechanism.

**A registry-owned WatchedItem cannot be deleted here.** `DELETE
/api/v1/watched-items/{id}` 409s once `applied_generation` is set, naming Archiver as
the authority: the stream is level-triggered, so the next announcement recreates the
row, and absence is not revocation — only a `revoked: true` tombstone retires a key.
Rows the registry has never announced still delete.

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
Two create paths since #254. `POST /api/v1/watched-items` requires all four of
`archiver_info_item_id` + `url` + `archiver_info_source_id` + a **non-empty**
`source_specs` (both ids validated as canonical uppercase ULIDs at the boundary,
a constraint the OpenAPI document advertises; `source_specs` became required in
#260 — a spec-less item has no defined extraction, and Archiver never provisions
one, so PATCH holds the same non-empty floor); **no dashboard create**. The
`info.registry` reconcile is the second: it creates from an announcement alone,
so a cold start converges from the snapshot without anyone calling the API — and
it is **not** gated on `source_specs`, because an announcement is authoritative
for that column ([CONTENT-PIPELINE.md](CONTENT-PIPELINE.md) → *Extraction
outcomes* has the residual that leaves). The POST no longer validates the
InfoItem over HTTP — that was watcher's last outbound call and it went with the
SDK — which makes the endpoint redundant once archiver#141's producer is live. The nullability had been paying for two
silent-drop branches on the SourceRevision path — both gone, so a captured
revision is always enqueued. Full detail, including why a fresh item starts
`unknown` rather than `probing`:
**[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)**. On any PATCH that sets
`effective_url` (the URL-succession path), `domain_name` is re-derived from the
URL **without** re-probing and `domain_suspended` is re-evaluated; every
create/PATCH/re-probe path (API and dashboard) shares
`ensure_domain_and_resolve_suspension` in
`src/core/domains.py` (#196). SourceRevisions are published to Archiver as
`source_revision_observed` facts on `content.revisions` (#253) on every detected
change; the local `pending_archiver_sync` outbox + drain worker guarantees
delivery during broker outages. Notifications dispatch inline from the pipeline **once per
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

## Operator surface and dashboard

The API/dashboard surface, its lifecycle guards, and the dashboard views built on
it — operator surface, dashboard parity, list view, domain counts, detail page,
and Recent Activity / Audit Log parity — are in
[WATCHED-ITEMS-DASHBOARD.md](WATCHED-ITEMS-DASHBOARD.md).

## Plans

Plans: the #191 collapse design is at [docs/plans/2026-06-16-collapse-watcheditem-watch-design.md](../docs/plans/2026-06-16-collapse-watcheditem-watch-design.md). Historical: design at [docs/plans/2026-05-15-watched-item-infoitem-first-design.md](../docs/plans/2026-05-15-watched-item-infoitem-first-design.md); #160 reshape at [docs/plans/2026-05-17-watched-item-watch-reshape.md](../docs/plans/2026-05-17-watched-item-watch-reshape.md); #161 CRUD UI at [docs/plans/2026-05-17-watched-item-crud-ui-plan.md](../docs/plans/2026-05-17-watched-item-crud-ui-plan.md). The Phase 5 cutover design ([docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md](../docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md)) is historical and was superseded by #160.
