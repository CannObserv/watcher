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
`last_checked_at`, `last_changed_at`, `last_observed_at` (#264 — advances when the
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
[docs/CONTENT-PIPELINE.md](CONTENT-PIPELINE.md)); and its
notification surface (the
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
source of truth used by **both** the pipeline (`process_watched_item` picks the
extractor) **and** the API (`WatchedItemResponse.media_type_essence` is a computed
field). `ServiceRegistry.get_extractor` maps essence → extractor from co-core's
`EXTRACTOR_BY_ESSENCE` — the same object Processor dispatches from, held by
reference so a co-core release that adds an essence reaches both sides at once
(#342) — and is total: anything unlisted falls back to co-core's
`DEFAULT_EXTRACTOR` (HTML). Read the table in co-core
(`co_core.pure.extract.dispatch`), not here. A dispatched
extractor that raises on mismatched bytes is caught as `ExtractionError` and
recorded like a fetch failure (ERROR health + `CHECK_EXTRACTION_FAILED` audit +
`WATCH_ERROR`), so a mislabeled non-HTML target surfaces a signal instead of
re-firing every `schedule_tick`.

## Domain keying

**Domain keying (#197).** `WatchedItem.domain_name` == `Domain.name` == `hostname(effective_url)` — the same string by construction (all derive from one `urlparse(...).hostname` over the same `effective_url`). That equality is what lets the fetch-policy producer publish per-`Domain.name` while items carry `domain_name`, and it is why `resolve_watch_target` derives the domain with the identical helper. **One entry per hostname** — host variants (`lcb.wa.gov` vs `www.lcb.wa.gov`) are independent by design. *History:* this used to describe the in-process `DomainRateLimiter`'s bucket key; the limiter retired with the local fetch path (#241 step 5) and per-host pacing is Replicator's, but the keying invariant still holds and is still load-bearing.

## Registry linkage and lifecycle

**Moved to [REGISTRY.md](REGISTRY.md).**

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

**Subscribable events (#166).** Three: `change_detected` (`src/workers/pipeline.py`),
`watch_error` and `watch_recovered` (`src/workers/fetch_commands.py`) — each
`WatchEventType` member is a Subscribe checkbox, so each must have a
`dispatch_event_notifications` site, and `tests/test_notification_dispatch_sites.py`
fails on one that does not. Created, paused, resumed, archived and deleted were
subscribable and never fired; #166 dropped them and migration `78606286a887`
stripped them from saved templates. Their **audits** (`EventType.WATCHED_ITEM_*`)
are a separate enum and stay. Pause is registry-owned in production
(the reconcile writes `is_active` directly), so if anyone notifies on it, Archiver does.

**Persistent errors re-notify (#71).** `watch_error` fires on the OK→ERROR
transition, then on the first failed check at least
`WATCHER_ERROR_RENOTIFY_INTERVAL` (default `24h`) after the last one.
`last_error_notified_at` is stamped in the failure's commit, before the dispatch
(a retried apply cannot re-send); recovery clears it. A reminder never
republishes watch-status (#264), carries `renotify: true` plus
`previously_notified_at`, and says "Still failing". Why not `schedule_config`,
and the backfill: `src/core/notifications/renotify.py`, migration `36358e2f5ad4`.

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

## Change diff (#222)

A `change_detected` body ends with a diff of the previous and current canonical
text, by **word** (#349): each change as `-`/`+` lines with 8 words of context,
wrapped at 72 columns, in a fenced block with no unified-diff header. Live items
extract to one long line, mostly without sentence ends, so #222's sentence split
and fixed-width wrap realigned everything after an edit; segments are now
content-defined, so an edit moves no natural boundary but its own (a positional
cut in a 64-word run without one can shift, up to the next natural boundary).
Page text stays
inside the fence (`_fence_for`, CR 13), never inline marks. Defaults: snippet on
(25 rendered lines, hunk-aware), full off (every change); both are bounded by
`MAX_RENDERED_DIFF_BYTES` (32 KiB, #346). Templates saved before #221 get their
stored diff choices back. On a change, the `include_last_changed_at` toggle
shows `PREVIOUS CHANGE` (`previous_changed_at`): `last_changed_at` is already
this change when the event is built. **Failure never blocks a notification** — the
body says `DIFF: unavailable (<reason>)`, `previous text not stored` for any
revision older than shadow mode. Each side is capped by
`WATCHER_DIFF_MAX_INPUT_BYTES`. Mechanism (lookup via the processor's
`output_uri`, hash check, the in-hand current text): `src/core/notifications/diff_loader.py`.

## WatchEvent identity fields

**WatchEvent identity fields** are `watched_item_id`, `item_name`, `item_url`
(renamed from `watch_*` in #191). The same names are the user-facing notification
template variables; the default-template "ITEM:" link (renamed from "WATCH:" in
#221) and `change_url` point at `/watched-items/{watched_item_id}` under
`app_url`, the dashboard's public base (`WATCHER_PUBLIC_BASE_URL`, #296 D6). With
no base configured both are omitted rather than guessed — a relative link is
useless in Slack or email. The
`AuditLog.watch_id` FK column was retired —
audits carry the WatchedItem as `watched_item_id` inside the JSONB `payload`
(filter via `GET /api/v1/audit?watched_item_id=<ulid>`).

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
