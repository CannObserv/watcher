# watcher — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Sacrifice grammar for density. Lead with the answer or action.

## Project Overview

Web service for monitoring cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff; Node.js + npm (for Tailwind CLI — `sudo npm install -g @tailwindcss/cli`, one-time VM setup).

## Code Exploration Policy

SocratiCode is indexed on this repo (`.socraticodecontextartifacts.json` present). Its MCP tools are **deferred** — schemas load only after a `ToolSearch` prefetch. The SessionStart hook prints the prefetch query; run it before exploring.

**Negative rule.** For broad semantic questions ("where is X", "how does Y work", "what depends on Z"), use SocratiCode MCP tools first. Reach for `grep`/`ripgrep` only on exact strings (error messages, log lines, known symbols). Reserve the Explore subagent for path-pattern walks (e.g. "all `*.py` under `src/api/routes/`"), not semantic search.

| Goal | Tool |
|------|------|
| Where is X defined / how does Y work / what files touch Z | `codebase_search` |
| Exact string/regex match (errors, log lines, known symbols) | `grep` / `rg` |
| Blast radius of changing/deleting a file or function | `codebase_impact` |
| What does an entry point actually do? | `codebase_flow` |
| Callers and callees of a function | `codebase_symbol` |
| List symbols in a file or search by name across the project | `codebase_symbols` |
| Imports/dependents of a file | `codebase_graph_query` |
| Spot circular deps or structural issues | `codebase_graph_circular`, `codebase_graph_stats` |
| Visualise module structure | `codebase_graph_visualize` |
| Verify index is up to date | `codebase_status` |
| DB schemas, deployment topology, runbook context | `codebase_context` / `codebase_context_search` |

Prefetch query (run via `ToolSearch` once per session if the SessionStart reminder isn't loaded):

`select:mcp__plugin_socraticode_socraticode__codebase_search,mcp__plugin_socraticode_socraticode__codebase_symbol,mcp__plugin_socraticode_socraticode__codebase_symbols,mcp__plugin_socraticode_socraticode__codebase_flow,mcp__plugin_socraticode_socraticode__codebase_impact,mcp__plugin_socraticode_socraticode__codebase_graph_query,mcp__plugin_socraticode_socraticode__codebase_graph_circular,mcp__plugin_socraticode_socraticode__codebase_graph_stats,mcp__plugin_socraticode_socraticode__codebase_graph_visualize,mcp__plugin_socraticode_socraticode__codebase_status,mcp__plugin_socraticode_socraticode__codebase_context,mcp__plugin_socraticode_socraticode__codebase_context_search`

## Project Layout

Top-level directories. Read the code for per-file detail.

```
src/api/         FastAPI app (ASGI routes, schemas, deps)
src/core/        Shared domain logic (models, probe, watches, notifications, diff, extractors, fetchers, scheduler, storage, crypto)
src/dashboard/   Server-rendered UI (Jinja2 + HTMX + Tailwind)
src/workers/     Procrastinate task queue (check_watch, schedule_tick, pipeline)
tools/           Operational scripts
tests/           Mirrors src/ structure
deploy/          Systemd units and deployment config
docs/            Reference docs (COMMANDS, DEPLOYMENT, SKILLS, STYLE) + plans/
scripts/         Build scripts (Tailwind, vendor CSS, cleanup)
skills/          Agent skills (committed overrides + symlinks → skills-vendor/)
skills-vendor/   Git submodules for external skill repos
.claude/skills/  Claude Code skill discovery (symlinks → ../../skills/<name>)
```

## Infrastructure

**Single-VM setup.** Dev and prod on the same VM. Code committed to `main` is the deployed code. Systemd service `watcher` runs the live site on port 8000.

| Service | Port | Managed by |
|---|---|---|
| API (live) | 8000 | `systemctl` (`watcher.service`) |
| API (dev) | 8001 | manual uvicorn |

Sibling services on the same VM, separately managed: **Archiver** at `/home/exedev/archiver` (port 8020, `archiver.service`); **Notifier** at `/home/exedev/notifier`.

The exe.dev proxy forwards 3000–9999. Dev server reachable at `https://watcher.exe.xyz:8001/`.

**Single process is load-bearing.** One uvicorn process runs everything: the API, the embedded Procrastinate worker, the config poller, and the cache sweeper (started in the `src/api/main.py` lifespan — there is no separate worker unit). The in-process `DomainRateLimiter` singleton holds all politeness state, so this topology is only correct at exactly one process. Never run `uvicorn --workers N` or a second worker unit against prod: each process would get its own rate-limiter, silently splitting every domain's politeness budget. Escalation path when one process stops being enough (not before): a separate `watcher-worker.service` plus Redis-backed rate-limiter state.

## Server Lifecycle

**Port 8000 belongs to systemd. Never start uvicorn manually on port 8000.**

After committing to `main`: `sudo systemctl restart watcher`. After DB model changes: `uv run alembic upgrade head` then restart. After Tailwind/vendor CSS changes: `bash scripts/build-css.sh` then restart. Logs: `sudo journalctl -u watcher -f`.

Dev server (port 8001, leaves prod alone):

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

**Archiver service.** Owns the canonical InfoItem / InfoSource / SourceRevision / RepSpec registry. Sibling repo at `/home/exedev/archiver` (extracted in #149). Watcher consumes it via the `archiver-client` SDK installed as a path dependency. Don't add Archiver code to this repo — go work in the sibling repo instead.

**Cross-repo policy.** Do not directly edit sibling repos (`archiver`, `notifier`) within a watcher conversation. If a change to a sibling is needed: identify the gap, recommend it, get approval, then file a GH issue in that repo. Implementation happens in a separate session scoped to the sibling.

Full lifecycle reference + cleanup timer: `docs/DEPLOYMENT.md`.

**Mirrored content-acquisition code.** Watcher and Archiver share copies of `src/core/fetchers/`, `src/core/extractors/`, `src/core/simhash.py`, `src/core/extraction_defaults.py`, and `src/core/logging.py`. When changing any of these here, mirror the change to `/home/exedev/archiver/src/core/`. Notifier-style discipline; revisit when fingerprint parity becomes load-bearing (i.e., when Replicator joins the consumer set).

## Environment Files

Two env files load in order (later overrides earlier):

1. `/etc/watcher/.env` — production secrets (`DATABASE_URL`, `NOTIFIER_API_KEY`, `ARCHIVER_API_KEY`, `REDIS_URL`). Persistent, managed manually on the VM.
2. `.env` (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`, `REDIS_URL`). Never commit.

Load both for shell commands:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
```

**Key variables:**
- `DATABASE_URL` — PostgreSQL connection for watcher (Archiver owns its own database).
- `REDIS_URL` — Redis connection URL (default: `redis://localhost:6379/0`). Override for testing or remote Redis.
- `NOTIFIER_BASE_URL` — Notifier service URL for the `NotifierClient` SDK (e.g. `http://localhost:9000`). Required — every notification is dispatched through the notifier service.
- `NOTIFIER_API_KEY` — Required. Watcher tenant API key issued by `scripts/seed_tenant.py` in the notifier repo.
- `ARCHIVER_BASE_URL` — Archiver service URL for the `ArchiverClient` SDK (default: `http://localhost:8020`).
- `ARCHIVER_API_KEY` — Required. API key for the `ArchiverClient` SDK; missing key crashes the API on boot (pre-warm in lifespan).
- `WATCHER_CACHE_DIR` — Scratch directory for SourceRevision bytes (default `/var/cache/watcher/scratch`). Must be writable by the `watcher` user; create on fresh hosts.
- `WATCHER_CACHE_TTL_SECONDS` — Scratch-file lifetime before the sweeper removes it (default `600`).
- `WATCHER_CACHE_SWEEP_INTERVAL_SECONDS` — Sweeper periodic interval (default `60`).

Full variable reference: `docs/DEPLOYMENT.md`.

## Watched Items

**The `WatchedItem` is the single monitored entity (#191).** The earlier
`WatchedItem → Watch` one-to-many was collapsed: `Watch` (the model, table,
`/watches*` routes, the override resolution chain, and the per-Watch
notification tier) is gone. One `WatchedItem` = one URL = one fingerprint = one
change signal. The user-facing noun is "Watched Item".

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
default (`resolved_schedule_config`, `src/core/watches/resolution.py`).
**Display** of the resolved interval + next-check goes through one helper,
`resolve_schedule_display` (`src/core/watches/schedule.py`, #206): it composes the
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

**Rate-limiter keying (#197).** The `DomainRateLimiter` throttle bucket is keyed
by domain *name*: `WatchedItem.domain_name` == `Domain.name` ==
`hostname(effective_url)` — the same string by construction (all derive from one
`urlparse(...).hostname` over the same `effective_url`). The fetch path
(`src/workers/tasks.py`) calls `acquire_for_domain` /
`report_rate_limited_for_domain` keyed on `WatchedItem.domain_name` (fallback
`hostname(effective_url)` is fail-safe, never fail-open); the config-load side
(startup hydration + poller `configure_domain`) keys on `Domain.name`, so the two
always agree. **One bucket per hostname** — host variants (`lcb.wa.gov` vs
`www.lcb.wa.gov`) are independent budgets by design; backoff on one does not slow
its siblings. A shared-budget alias layer is deferred until real throttle bleed
is observed. URL-keyed `acquire(url)`/`report_rate_limited(url)`/`extract_domain(url)`
were removed in #197 (were dead code).

`archiver_info_item_id` on `WatchedItem` is nullable — WatchedItems created via the
dashboard (`POST /watched-items/new`) have no InfoItem reference; API-created ones
may also omit it when using the URL-only path. `effective_url`
and `domain_name` are set at create time by probing the URL (no
Archiver SDK call per cycle). On the InfoItem-linked create and on any PATCH
that sets `effective_url` (the Archiver "Begin Watching" / URL-succession
paths), `domain_name` is re-derived from the URL **without** re-probing —
Archiver is authoritative for `effective_url` — and `domain_suspended` is
re-evaluated; every create/PATCH/re-probe path (API and dashboard) shares
`ensure_domain_and_resolve_suspension` in `src/core/domains.py` (#196). SourceRevisions are POSTed to Archiver via the
`archiver-client` SDK on every detected change; the local
`pending_archiver_sync` outbox + drain worker guarantees delivery during
Archiver outages. Notifications dispatch inline from the pipeline **once per
WatchedItem** on change detection (`notifications_dispatched ≤ 1`), with
`change_revision_id` in WatchEvent metadata. `schedule_tick` skips items that
are paused (`is_active=false`), archived, or `domain_suspended`, and applies the
temporal profile's post-actions (deactivate / archive / reduce_frequency) to the
WatchedItem itself.

**Notifications (#200).** One table — `notification_templates` — holds every
notification target. Each `NotificationTemplate` has an intrinsic `visibility`
that controls where it fires:

- `global` — every WatchedItem (`domain_name`/`watched_item_id` both NULL).
- `domain` — every WatchedItem whose `domain_name` matches.
- `watched_item` — the single `watched_item_id` only.

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

CRUD: generic visibility-aware library at `/api/v1/notifications/templates`
(create takes `visibility` + the matching ref); item-scoped convenience at
`/api/v1/watched-items/{id}/notifications` (creates `visibility='watched_item'`),
with `GET .../effective` returning the full in-scope set (global + the item's
domain + the item) — the single answer to "which channels fire for this item".
Dashboard: the library `/notifications` create makes global templates; domain
templates are created from the domain detail page; item templates from the item
detail page. Design: [docs/plans/2026-06-19-notification-model-consolidation-design.md](docs/plans/2026-06-19-notification-model-consolidation-design.md).

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

**WatchEvent identity fields** are `watched_item_id`, `item_name`, `item_url`
(renamed from `watch_*` in #191). The same names are the user-facing notification
template variables; the default-template "ITEM:" link (renamed from "WATCH:" in
#221) and `change_url` point at `/watched-items/{watched_item_id}`. The
`AuditLog.watch_id` FK column was retired —
audits carry the WatchedItem as `watched_item_id` inside the JSONB `payload`
(filter via `GET /api/v1/audit?watched_item_id=<ulid>`).

Fresh hosts need the scratch directory: `sudo mkdir -p /var/cache/watcher/scratch && sudo chown watcher:watcher /var/cache/watcher/scratch` (or override via `WATCHER_CACHE_DIR`). The Archiver service must also be installed first — see its own `docs/DEPLOYMENT.md`. Archiver authoring tools (`validate_source_spec`, `fetch_and_render`, `preview_extraction`, `propose_selectors`, `find_info_item`, atomic `create_info_item`) are documented in `/home/exedev/archiver/AGENTS.md`.

Operators manage WatchedItem defaults (`name`, `description`, `default_schedule_config`, `default_tags`; `content_media_type` is auto-detected but overridable — #168), archive/restore lifecycle, and notification-template CRUD via the `/watched-items` dashboard. Same surface is exposed at `/api/v1/watched-items`. WatchedItems are created at `POST /api/v1/watched-items` (accepts `archiver_info_item_id` or `url`; both optional but at least one required) or `GET/POST /watched-items/new` (dashboard — URL-first; an `is_active` checkbox provisions paused). Create and PATCH accept `is_active` (#188): create defaults `true`; pass `false` to provision paused. `is_active` is the **pause/resume** toggle (distinct from archive) — paused (`is_active=false`, not archived) items are skipped by `schedule_tick` and short-circuited by the `check_watched_item` task, but stay editable. PATCH `is_active` on an archived item is rejected (409 — restore first); activation while archived is owned by archive/restore. Archive stamps `archived_at` and flips `is_active` (single entity — no child cascade since #191); restore clears `archived_at` and re-activates. **Permanent delete (#210):** `DELETE /api/v1/watched-items/{id}` → 204; **archived-only** (409 `"WatchedItem must be archived before deletion"` otherwise; 404 unknown/malformed). DB-level `ON DELETE CASCADE` removes the four children (`temporal_profiles`, item `notification_templates`, `change_revisions`, `pending_archiver_sync`); a `WATCHED_ITEM_DELETED` audit row is written **before** the delete and survives it (id lives in the JSONB payload, no FK). Local-only — Archiver-side InfoItem/SourceRevisions are untouched. Deleting the last archived item on a domain frees the #209 domain delete guard. Filter by InfoItem with `GET /api/v1/watched-items?archiver_info_item_id=<ulid>`. Trigger an immediate check with `POST /api/v1/watched-items/{id}/check-now` (202; pre-flight: not archived, not paused, has `effective_url`).

**Dashboard parity (#190):** the dashboard surfaces pause/resume (`POST /watched-items/{id}/toggle-active` — mirrors the API 409 guards, blocks resume while `domain_suspended`, emits the `WATCHED_ITEM_PAUSED`/`RESUMED` events), check-now (`POST /watched-items/{id}/check-now` — delegates to the API route, guard failures flash), effective_url editing (`POST /watched-items/{id}/effective-url` — re-probes to re-derive `domain_name`, leaves `source_specs` untouched), and permanent delete (`POST /watched-items/{id}/delete` — delegates to the API DELETE; the **Delete permanently** control lives in the archived branch of the detail Danger Zone only, redirects to `/watched-items` on success, surfaces the 409 as an OOB flash; #210). Pause/resume + check-now controls appear on the WatchedItem detail page and in the list rows. `source_specs` is shown read-only on detail (authoring stays in Archiver tooling). The detail page surfaces a single item-template panel (item-scoped `NotificationTemplate` CRUD) plus read-only Global/Domain inherited sections; the full API surface lives at `/api/v1/watched-items/{id}/notifications` (see **Notifications**).

**Watched Items list view** (`#172`, `#173`, `#190`): columns are Name → Last Check → Interval → Next Check → Status → Actions (per-row pause/resume toggle + check-now). The Status badge distinguishes Active / Paused / Domain Inactive / Archived. Interval and Next Check resolve through `resolve_schedule_display` (the #206 helper over the 3-tier `resolved_schedule_config`; #204, #205, #206), so an item with no explicit `default_schedule_config` shows the inherited interval with a source marker (`· domain` when inherited from the Domain cadence, `· default` from the system default, `· profile` when an active `TemporalProfile` overrides the base cadence) plus a computed Next Check rather than blanks — parity with the detail page and consistent with `schedule_tick`. Next Check is a live countdown rendered by `src/dashboard/static/js/next-check-countdown.js` (loaded globally via `base.html`; reads `data-next-check` ISO timestamp attributes, refreshes every 60 s). List has server-side name search and pagination: `GET /partials/watched-items-table?q=&page=&page_size=&include_archived=` is the HTMX partial; the full page (`GET /watched-items`) accepts the same params and SSR-includes the partial on first load. Active/All archived toggle is a segment-group that cross-includes the search input. Aspect Review column removed (#173) — too expensive per-row; will surface on WatchedItem detail page behind a Redis cache (tracked in #163).

**Domain WatchedItem counts (#209):** the Domains-list "Watched Items" column and the domain-detail heading count *live* (non-archived) items only, and the list's "Last Checked" `max` excludes archived items too. The detail heading shows the archived remainder explicitly (`N · M archived`). The domain **delete guard** is the one place that counts the archived-inclusive total — archived WatchedItems still hold the `domain_name` FK, so deletion stays blocked while any reference exists.

**InfoItem picker removed** (`#185 Phase A step 7`): the InfoItem typeahead picker (routes `GET /info-items/search`, `GET /info-items/{id}/binding-tree`; JS `info-item-picker.js`; templates `partials/info_item_picker/`) was removed. WatchedItem-create accepts a URL directly and probes it for `effective_url` + `domain_name` (the separate Watch-create flow no longer exists — #191).

**Watched Item detail** (`#174`, updated `#185`, `#190`, `#191`, `#199`, `#202`, `#215`): the heading carries a subdued **Watched Item** eyebrow. A single **Details** panel holds, in order, Name, the `effective_url` row (inline re-probe **Edit**), Domain, the Status pause/resume toggle (the sole status badge), `last_checked_at` (with the Check-now action and the **Health** badge inline — health is the result of that check, surfaced with an accessible hover/focus tooltip), `last_changed_at`, Interval (when unset, shows the inherited cadence with a `· domain`/`· default` source marker, or `· profile` when an active temporal profile overrides it; #205, #206), Content Type, Description, and Tags — all from local WatchedItem columns, no Archiver SDK calls. Below it: a read-only `source_specs` panel, then the **Notification Templates** panel (item-scoped CRUD plus read-only **Global**/**Domain** inherited sections that fire at dispatch — parity with the Domain detail page; #199, unified in #200), then **Recent Activity**. `POST /watched-items/{id}/mark-reviewed` (stamps `last_reviewed_at`) remains API-only — the dashboard route exists but is intentionally unwired; no dashboard UI until a replacement is designed.

**Recent Activity / Audit Log parity (#215).** The detail page's **Recent Activity** section and the global **Audit Log** (`/audit`) share one chip-filter partial (`partials/audit_filter_chips.html`) and one table partial (`partials/audit_table.html`), both driven by the single HTMX endpoint `GET /partials/audit-table`. The endpoint is scoped by `watched_item_id`: when present (detail page), it filters to that item, hides the redundant **Watched Item** column (`show_watched_item=False`), and targets `#wi-activity-table`; when absent (Audit Log), it shows all events with the column and targets `#audit-table`. Both surfaces paginate via `partials/pagination.html` over `get_audit_entries` / `get_audit_entries_count` (`src/dashboard/context.py`); the route helper `_audit_table_context` is the single source of the render+pager context. The pager's footer style is context-aware via a `sticky` flag (`= not item_scoped`): the item-scoped detail Recent Activity renders a flush, non-sticky footer inside the standard bordered card (parity with the sibling detail panels, dark fill `gray-800` to match; #223), while the global `/audit` keeps the viewport-anchored sticky footer (`gray-900`, matching the page). HTMX drives filtering/paging, but the page routes (`/audit` and the detail page) also honor `?event_type`/`page`/`page_size` query params, so the chip filter's no-JS Apply button and deep-links work. The event-type chips are **multi-select, OR-matched**: `event_type` is a repeatable query param (`?event_type=a&event_type=b` → `event_type IN (a, b)`), each chip change submits the whole form (`hx-include="closest form"`) so every checked chip is sent, and **Clear filter** is a full-page link (it must reset the checkboxes, which live outside the swapped table region). Pagination params are clamped to safe bounds by `clamp_pagination` (`src/dashboard/deps.py`, `PAGE_SIZES = (25, 50, 100)`) — shared by every paginated dashboard list route (watched-items, domains, audit) so a crafted `?page_size=-5`/`?page=-5` can't reach the DB as a negative/unbounded `LIMIT`/`OFFSET` (#215 CR-6). Chip choices differ by surface: the **global Audit Log derives its chips dynamically** from the event types actually present (`get_distinct_audit_event_types` → `SELECT DISTINCT event_type … ORDER BY event_type`, alphabetical/prefix-grouped), so the filter always matches the data — no dead chips, no missing chips (#217). On an unbounded `audit_log` all three per-page queries are index-backed (#218): the dominant `ORDER BY created_at DESC` list by `ix_audit_log_created_at`, and both the `event_type IN (...)` filter and this DISTINCT-chip query by the composite `ix_audit_log_event_type` `(event_type, created_at DESC)` (the WatchedItem-scoped filter keeps using the #193 `ix_audit_log_payload_watched_item_id`). The **per-item Recent Activity** uses the curated static `WATCHED_ITEM_EVENT_CHOICES` (the `check.*` + `watched_item.*` subset a single item emits). The legacy `watch.*` event prefix (retired in #191) was purged — the `EventType.WATCH_*` constants and the stray `audit_log` rows are gone (pre-production cleanup). The old friendly-summary list (`get_watched_item_activity`, `_WI_ACTIVITY_SUMMARY`) was retired — rows now show the raw event badge + a **Details** `View` action, matching the Audit Log. **Details View (#216):** the Details cell is a `View` button (or `—` for an empty payload) that expands a hidden full-width sibling `<tr>` (`colspan` = 4 with the Watched Item column / 3 without) showing the **pretty-printed** payload (`tojson(indent=2)`) read-only, styled like the Source Specs `<pre>` block, with a `Close` button. Toggling is delegated on `document` in `static/js/audit-details-toggle.js` (loaded globally via `base.html`) so it survives HTMX table swaps; the `View` button carries `aria-expanded`/`aria-controls`, and `Close` returns focus to it.

Plans: the #191 collapse design is at [docs/plans/2026-06-16-collapse-watcheditem-watch-design.md](docs/plans/2026-06-16-collapse-watcheditem-watch-design.md). Historical: design at [docs/plans/2026-05-15-watched-item-infoitem-first-design.md](docs/plans/2026-05-15-watched-item-infoitem-first-design.md); #160 reshape at [docs/plans/2026-05-17-watched-item-watch-reshape.md](docs/plans/2026-05-17-watched-item-watch-reshape.md); #161 CRUD UI at [docs/plans/2026-05-17-watched-item-crud-ui-plan.md](docs/plans/2026-05-17-watched-item-crud-ui-plan.md). The Phase 5 cutover design ([docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md](docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md)) is historical and was superseded by #160.

## Common Commands

```bash
uv sync                                      # install deps
uv run pytest                                # tests
uv run pytest -m integration                 # integration tests (needs PostgreSQL)
uv run ruff check .                          # lint
uv run alembic upgrade head                  # apply migrations
uv run alembic revision --autogenerate -m "description"
```

Full reference: `docs/COMMANDS.md`.

### Tests require the Archiver sibling repo

Watcher's `tests/conftest.py` provisions the cross-schema `information.*`
test tables by subprocess-invoking Archiver's own alembic against
`TEST_DATABASE_URL`. Without the sibling repo on disk, tests fail at
session start with "Archiver repo not found at /home/exedev/archiver".

Setup:

```bash
# Default location:
git clone <archiver-repo> /home/exedev/archiver
cd /home/exedev/archiver && uv sync
```

Override via `ARCHIVER_REPO_PATH=/some/other/path` if you keep the
sibling repo elsewhere.

The `information` schema persists between pytest sessions to enable
the cache-check (#150); per-test row isolation is still handled by
`db_session`'s savepoint rollback. Tests that bypass `db_session` and
write directly via `engine.connect()` will leak rows into subsequent
sessions — don't do that.

**pytest-xdist is unsupported.** The fixture writes to a single
`TEST_DATABASE_URL`; multiple xdist workers would race on `alembic
upgrade head` and on watcher-table teardown. Tracked in #150 —
worker-id-suffixed databases + a coordination lock are the rework when
xdist is actually adopted.

## Agent Skills

Skills live in `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Local overrides in `skills/` shadow vendor submodules in `skills-vendor/`.

| Skill | Triggers / when to invoke |
|---|---|
| `reviewing-code-python-fastapi` | CR, code review |
| `reviewing-architecture` | AR, architecture review |
| `shipping-work-python-fastapi` | ship it, push GH, close GH, wrap up |
| `brainstorming` | brainstorm, design this, let's design |
| `writing-plans` | write plan, implementation plan |
| `writing-skills` | write skill, new skill, author skill |
| `systematic-debugging` | any bug, test failure, unexpected behavior |
| `verification-before-completion` | before any completion claim or commit |
| `test-driven-development` | before writing implementation code |
| `subagent-driven-development` | dispatch agents for plan execution |
| `dispatching-parallel-agents` | 2+ independent tasks in parallel |
| `using-git-worktrees` | feature work needing isolation |
| `managing-skills` | add skill repo, manage external skills |
| `socraticode` (codebase MCP) | see **Code Exploration Policy** above |

Full skill reference: `docs/SKILLS.md`. Cross-project search to the sister `notifier` index requires a per-instance `.claude/settings.local.json` (gitignored) — see "Linked Projects" in `docs/SKILLS.md`.

## Conventions

**Commit Messages:**
```
#<number> [type]: <description>      # with issue
[type]: <description>                # without issue
```
Types: feat, fix, refactor, docs, test, chore.

**Logging:**
```python
from src.core.logging import get_logger
logger = get_logger(__name__)
```
Entry points only: call `configure_logging()` once.

**Date & Time:** All UTC. ISO 8601: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (timestamps), `YYYY-MM-DD` (dates).

**General:**
- No inline module imports; all at file top
- Docstrings for public modules, classes, functions
- Test structure mirrors source (`src/foo.py` → `tests/test_foo.py`)
- Explicit imports only
- Small, focused functions
- Optional JSONB columns: declare as `JSONB(none_as_null=True)` so Python `None` persists as SQL `NULL`, not a JSONB `'null'` literal (otherwise `WHERE col IS NULL` silently misses those rows — #198)

**ULID format errors:** Treatment depends on whether the ULID is a path parameter or a filter query parameter.

- **Path parameters** (e.g. `/watched-items/{id}`) → 404. Use `parse_ulid` from [src/api/routes/helpers.py](src/api/routes/helpers.py), which raises `HTTPException(404)`. The dashboard helper (`get_watched_item_detail` in [src/dashboard/context.py](src/dashboard/context.py)) returns `None` and the route renders a 404 page.
- **Filter query parameters** on list endpoints (e.g. `?watch_id=<value>`) → 400. Use `parse_filter_ulid` from [src/api/routes/helpers.py](src/api/routes/helpers.py), which raises `HTTPException(400, "Invalid <field> format")`. Pass the parameter name as `field` (e.g. `parse_filter_ulid(watch_id, "watch_id")`).

Do not use `parse_ulid` for filter query params — the endpoint itself exists; an unparseable filter value is a bad request, not a missing resource.

**DB Triggers (gotcha):**
- Triggers live in Alembic migrations (`CREATE OR REPLACE FUNCTION` + `CREATE OR REPLACE TRIGGER`; downgrade with `DROP TRIGGER IF EXISTS … ON table; DROP FUNCTION IF EXISTS …`).
- Integration tests use `Base.metadata.create_all` (not migrations), so triggers are NOT applied automatically. Any trigger added in a migration must also be recreated in `tests/conftest.py` inside the `test_engine` fixture, after `create_all`.
- Current triggers: none. `trg_changes_update_last_changed_at` removed in Phase 5 (#156) when the `changes` table was dropped.

## Style & UI

Authoritative reference: `docs/STYLE.md`.

**Brand:** Cannabis Observer — `co-purple-600` (#6d4488) primary accent. Never use brand colors for semantic status (green/yellow/red/blue).

**Dark Mode:** Tailwind `dark:` variants on every color utility. Class-based toggle (`<html class="dark">`). localStorage key: `watcher-color-scheme`.

**Accessibility:** WCAG 2.1 AA. Skip link, ARIA landmarks, `focus-visible` rings, 44px touch targets, `aria-live` on HTMX swap targets, reduced motion. Wrap decorative emoji in `<span aria-hidden="true">`. No `title` attributes. **Touch-target idiom (#203):** component classes (`.btn*`, `.segment`, `.chip`, `.form-input`, `.toggle`, nav-link) own the 44px guarantee — never restate `min-h-[44px]` on a `.btn`; use it only on bare interactive elements (`<a>`, `<label>`, component-less `<button>`); never `min-h-0`. Guard: `tests/dashboard/test_touch_targets.py` + `scripts/check-touch-targets.sh`. See `docs/STYLE.md` §7.

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.segment-group`, `.segment`, `.chip-group`, `.chip`, `.detail-grid`, `.toggle`, `.danger-zone`). Badge variants: `.badge-active` (green), `.badge-inactive` (gray), `.badge-archived` (amber), `.badge-error` (red), `.badge-warning` (orange), `.badge-info` (blue). Use CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. Detect HTMX via the canonical `is_htmx(request)` helper ([src/dashboard/deps.py](src/dashboard/deps.py)) — `HX-Request` header with `HX-Boosted` guard, so a boosted full-page nav stays on the non-HTMX path — never a bare `request.headers.get("HX-Request")` read (guarded by `tests/dashboard/test_htmx_detection.py`; #211). All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.
