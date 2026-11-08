# Watched Items

Everything the `WatchedItem` entity owns: fields, schedule resolution, registry
reconciliation. The operator surface that renders it and the
guards on its lifecycle are in
[WATCHED-ITEMS-DASHBOARD.md](WATCHED-ITEMS-DASHBOARD.md). `AGENTS.md` carries
the one-entity rule, the create path, and the handful of invariants an agent
needs on nearly every task; the detail is here.

## Fields and schedule resolution

A `WatchedItem` owns everything: the canonical `effective_url` and `source_specs`
used by the pipeline; `default_schedule_config` (the operator's item cadence, inert since F2, #375), `default_tags`;
`content_media_type` (#168); `domain_name` (FK → `Domain.name`, set at create time);
`domain_suspended` (set True/False by domain deactivation/reactivation, through
`set_domain_active` — the dashboard toggle and the `src.ops.suspend_domain`
break-glass, #372 — it gates scheduling directly, no live Domain join); `domain_default_schedule_config`
(denormalized copy of the parent Domain's cadence — was the Domain tier of schedule
resolution, #205; inert since F2, #375); a single optional
`TemporalProfile` (1:1, `temporal_profiles.watched_item_id`; inert since F2 —
Archiver evaluates profiles, and the row is kept for #380); `health_status`;
`health_reason` / `health_status_code` (#370 — why it is in ERROR now: the latest
failed check's `WatchHealthReason` token and, for `http_status`, the origin's
status; written on every failure, cleared on leaving ERROR, published on
`info.watch-status` beside `last_full_fetch_at`); `error_since` (#369 — when the
current error episode began: stamped on the edge into ERROR, cleared on leaving it,
sent as `WatchRecoveredEvent.error_since` on `info.watch-events`, NULL for an
episode opened before it existed — [WATCH-EVENTS.md](WATCH-EVENTS.md)); `last_checked_at`, `last_changed_at`, `last_observed_at` (#264 — advances when the
content was verified current: a successful check, changed or unchanged alike, **and**
an origin 304 where no extraction ran at all (#249); `last_checked_at` advances
on *every* outcome because it is the anti-thrash scheduling stamp (#168), so the
pair distinguishes "content verified current as of T" from "we tried at T" —
next-due derives from the latter, never the former. Both are on
`WatchedItemResponse` and on the detail page's Details panel, the latter as
**Content Verified** beside **Last Checked** (#266); the row does not split the
two provenances, because the column stores the instant and not the evidence —
that lives in the audit trail, where `CHECK_NO_CHANGE` carries
`source: not_modified`); `last_full_fetch_at` (#269 — the third stamp of the
freshness set: when *bytes* last arrived, advanced only by a blob apply, so a
304 moves the other two and not this one; the gap between it and
`last_observed_at` is how long the item's fingerprint has been inherited rather
than recomputed, and the detail page shows it as **Last Full Fetch**);
`blob_expires_at` (#339 — that fetch's blob horizon, stamped with it; replay
stops at the half-life between the two); the
conditional-GET validator state `etag` / `last_modified` / `validator_source_key`
(#269 — the pair the next command replays and the identity of what the bytes were
going to mean when it was stored; see
[docs/CONDITIONAL-GET.md](CONDITIONAL-GET.md)); `processor_version` (#326 —
the extraction identity of the latest successful outcome, refreshed even when
the fingerprint is unchanged: Option A's comparison base, and the key's
generation when the processor decides — see
[docs/CONTENT-PIPELINE.md](CONTENT-PIPELINE.md)). **Schedule resolution is announced, else the system default (#375, F2).**
`resolved_schedule_config` (`src/core/scheduling/resolution.py`) returns
`announced_schedule_config` when set (an explicit `{}` passes through as "no
interval"), otherwise `SYSTEM_DEFAULT_SCHEDULE_CONFIG` (`1d`). Archiver resolves
profile → item → domain and announces a concrete interval
(CannObserv/archiver#364), and runs the daily profile boundary with its
post-actions (CannObserv/archiver#365).

*Before F2*, resolution was four tiers under a floor (#205, #254):
`announced_schedule_config` → WatchedItem `default_schedule_config` → the Domain
default (the denormalized `domain_default_schedule_config`) → system default, then
`max(resolved, throttle_floor_interval)`. `schedule_tick` also evaluated each
item's active `TemporalProfile` — a ramp interval inside its window, and once the
date had passed a post-action: `deactivate` and `archive` wrote `is_active` /
`archived_at` (without the registry guard), `reduce_frequency` set the floor to
`1d` and audited `WATCHED_ITEM_THROTTLED`. The `/api/v1/watched-items/{id}/profiles`
routes managed profiles. F2 deleted the tiers' reads, the floor, the profile
evaluation, the post-actions and the routes; the columns, the `temporal_profiles`
table and the writers stay, inert, until the jump (#377/#378 delete the surfaces,
#380 the columns) — `src/ops/export_for_archiver.py` still reads them all.

**Display** of the resolved interval + next-check goes through one helper,
`resolve_schedule_display` (`src/core/scheduling/schedule.py`, #206): it composes
`resolved_schedule_config` and `compute_next_check`, returning a `ScheduleDisplay`
(`interval_text`, `source` `registry`/`default`, `next_check`, plus a `marker`
property that renders the source). Every surface — list (`build_schedule_map`),
detail interval field, and the domain-detail table — renders from it, so the UI
matches `schedule_tick`. Before F2 it also reported the `item`/`domain` sources, a
`throttled` flag and an active profile's override (`· profile`). Both domain facts
(`domain_suspended`, `domain_default_schedule_config`) are still denormalized onto
the WatchedItem via `ensure_domain_and_resolve_suspension` on every create/PATCH
path and back-filled across a domain's items on domain edit
(`backfill_domain_schedule_config`); only `domain_suspended` is read now.

Cadence is validated at the API write boundary by the same helper as the Domain
boundary (`validate_optional_schedule_config`, #205): a non-`None` config must carry
a parseable `interval`, and `{}` is rejected — delegation has exactly one spelling,
`None`/omit (the direction cannobserv#324 settled for the registry document). The
rule was held there because `schedule_tick` resolves every item in one task, so an
unparseable stored interval would raise out of `compute_next_check` and stop
scheduling for the whole system. Since F2 those writes reach inert columns; the
reconcile's own parse is what keeps the announced column safe.

Per-domain cadence is `Domain.default_schedule_config`
(a `schedule_config` interval string — operator check cadence, distinct from the
`Domain.min_interval` rate-limiter floor), still editable via `PATCH
/api/v1/domains/{name}` and the domain detail page, and inert since F2 (#375): the
dashboard field says so. Archiver holds the domain default it resolves from.

## Registry reconciliation (#254)

**Moved to [REGISTRY.md](REGISTRY.md).**

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
source of truth used by **both** the issuer (the `content.process` command
carries it; the processor picks the extractor, #350) **and** the API
(`WatchedItemResponse.media_type_essence` is a computed field). An extractor
that fails on mismatched bytes comes back as a terminal failure and is recorded
like a fetch failure (ERROR health + `CHECK_EXTRACTION_FAILED` + `WATCH_ERROR`).

## Domain keying

**Domain keying (#197).** `WatchedItem.domain_name` == `Domain.name` == `hostname(effective_url)` — the same string by construction (all derive from one `urlparse(...).hostname` over the same `effective_url`). That equality is what let the fetch-policy producer (removed in #374) publish per-`Domain.name` while items carry `domain_name`, and it is why `resolve_watch_target` derives the domain with the identical helper. **One entry per hostname** — host variants (`lcb.wa.gov` vs `www.lcb.wa.gov`) are independent by design. *History:* this used to describe the in-process `DomainRateLimiter`'s bucket key; the limiter retired with the local fetch path (#241 step 5) and per-host pacing is Replicator's, but the keying invariant still holds and is still load-bearing.

## Registry linkage and lifecycle

**Moved to [REGISTRY.md](REGISTRY.md).**

## Notifications: Archiver's (#376, #379)

**Watcher sends no notification.** Archiver has sent every notification for an
event at or after T = `2026-11-06T21:00:00Z`, from the `info.watch-events` facts
Watcher stages (#369) — [WATCH-EVENTS.md](WATCH-EVENTS.md). #376 deleted
Watcher's dispatcher, its three sites, #71's error reminder and the T gate;
#379 deleted the rest: the templates' renderer and change diff, the test sends,
the `/api/v1` and dashboard template surfaces, and the notifier client.
`tests/test_notification_subsystem_retired.py` fails on any of it coming back.

**Kept, inert, for #380:** the `NotificationTemplate` model and its
`notification_templates` table (one row per target, with an intrinsic
`visibility` — `global`, `domain` or `watched_item`, #200), which
`src/ops/export_for_archiver.py` still re-exports, and the audit
`EventType.NOTIFICATION_*` strings. Deleting an item still cascades to its
item-scoped rows.

Audits carry the WatchedItem as `watched_item_id` inside the JSONB `payload`
(filter via `GET /api/v1/audit?watched_item_id=<ulid>`); the `AuditLog.watch_id`
FK column was retired.

## What the #191 collapse removed

The `Watch` model is gone — its table, its
`/watches*` routes, the override resolution chain and the per-Watch notification
tier with it. `AGENTS.md` carries the one-entity rule itself; this is the list of
what an agent will not find.

## Operator surface and dashboard

The API/dashboard surface, its lifecycle guards, and the dashboard views built on
it — operator surface, dashboard parity, list view, domain counts, detail page,
and Recent Activity / Audit Log parity — are in
[WATCHED-ITEMS-DASHBOARD.md](WATCHED-ITEMS-DASHBOARD.md).

## Plans

Plans: the #191 collapse design is at [docs/plans/2026-06-16-collapse-watcheditem-watch-design.md](../docs/plans/2026-06-16-collapse-watcheditem-watch-design.md). Historical: design at [docs/plans/2026-05-15-watched-item-infoitem-first-design.md](../docs/plans/2026-05-15-watched-item-infoitem-first-design.md); #160 reshape at [docs/plans/2026-05-17-watched-item-watch-reshape.md](../docs/plans/2026-05-17-watched-item-watch-reshape.md); #161 CRUD UI at [docs/plans/2026-05-17-watched-item-crud-ui-plan.md](../docs/plans/2026-05-17-watched-item-crud-ui-plan.md). The Phase 5 cutover design ([docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md](../docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md)) is historical and was superseded by #160.
