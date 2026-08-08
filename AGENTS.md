# watcher — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Sacrifice grammar for density. Lead with the answer or action.

## Project Overview

Web service for monitoring cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff; Node.js + npm (for Tailwind CLI — `sudo npm install -g @tailwindcss/cli`, one-time VM setup).

**Cannobserv wheelhouse (#220).** `co-core` + `co-core-aio` (the shared cannabis-observer substrate) resolve from a local wheelhouse mirrored from the private GCS index `gs://co-gcs-pypi`, via `[tool.uv] find-links = ["./.wheelhouse"]` — **not** git sources. Populate it **before any `uv` command** (find-links makes every `uv` invocation require the dir; `.wheelhouse/.gitkeep` is tracked so a fresh clone has it):

```bash
uv run --no-project --with 'google-cloud-storage>=2,<4' python scripts/sync_wheelhouse.py
uv sync
```

Auth is ADC: on the VM/deploy the `co-pypi-reader` SA key at `GOOGLE_APPLICATION_CREDENTIALS` (in `/etc/watcher/.env`); in CI, keyless via Workload Identity Federation (`.github/workflows/ci.yml`). The identity needs only `roles/storage.objectViewer`. Reproducibility is `uv.lock` (pins the exact version), not wheelhouse contents. **Upgrade:** re-sync, then `uv lock --upgrade-package co-core` (bump the floor if the minor moved). Currently pinned: **v0.8.0** (floors `>=0.8.0,<0.9`; `co-core-aio` carries the **`bus`** extra for the fetch-policy producer — #245). The 0.8 bump (cannobserv#300, adopted in #252) is **breaking in both directions**: `info_source_id` is required on all three content contracts and `BlobAvailableEvent.command_id` stopped being optional, so a 0.7.7 fact no longer decodes here — see **Phase 4 contracts** for the deploy-ordering rule that follows from it. `co-core` carries the **`extract`** extra (`co-core[extract]`) — the heavy HTML/PDF/CSV parsers behind the extractors constructed in `src/core/registry.py`. The content-acquisition pipeline (fetch → extract → fingerprint) is now co-core's (`co_core.pure.extract.*`, `co_core.effects.fetch`, `co_core_aio.fetch`), adopted in #236; watcher no longer fetches at all (#241 step 5) — `src/core/fetch.py` is gone; the `watcher/0.1.0` User-Agent now lives in `src/core/fetch_commands.py` beside its only consumer and rides out on every `content.fetch` command's headers to preserve fingerprint byte-continuity. The systemd unit refreshes the wheelhouse via a non-fatal `ExecStartPre` so restarts self-heal (its output is plain text, not the app's JSON — see **Logging**).

## Code Exploration Policy

SocratiCode is indexed on this repo (`.socraticodecontextartifacts.json` present). Its MCP tools are **deferred** — schemas load only after a `ToolSearch` prefetch. The SessionStart hook prints the prefetch query; run it before exploring.

**Index scope (#240).** `.socraticodeignore` (repo root, gitignore syntax) keeps the
vendored skill trees — `skills-vendor/` and the `.claude/skills/` symlink farm — out of
the semantic index; vendor prose otherwise outranks this repo's own code in
`codebase_search`. `skills/` stays indexed: it holds the committed first-party overrides,
and its vendor symlinks resolve to `skills-vendor/` paths that are already excluded.
Editing the file only changes what *subsequent* scans pick up — chunks embedded by an
earlier index survive it (vendor hits kept ranking after the file landed). Purging them
takes a clean rebuild: `codebase_remove` then `codebase_index`, which re-embeds the whole
repo — budget a maintenance window for it.

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
src/core/        Shared domain logic (models, probe, scheduling, notifications, diff, fetch commands, storage, crypto)
src/dashboard/   Server-rendered UI (Jinja2 + HTMX + Tailwind)
src/workers/     Procrastinate task queue (check_watched_item, schedule_tick, pipeline, fetch apply/consumer)
tools/           Operational scripts
tests/           Mirrors src/ structure
deploy/          Systemd units and deployment config
docs/            Reference docs (COMMANDS, CONTENT-PIPELINE, DEPLOYMENT, SKILLS, STYLE) + plans/
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

Sibling services on the same VM, separately managed: **Archiver** (port 8020, `archiver.service`) and **Notifier**. Both are separate repos checked out alongside this one (`/home/exedev/archiver`, `/home/exedev/notifier` on this VM). Elsewhere in these docs they're named as "the Archiver repo" / "the Notifier repo" — resolve those against your own checkout.

**The Archiver checkout location is not freely relocatable.** Two independent consumers resolve it, and only one takes an override:

- `pyproject.toml` → `[tool.uv.sources]` pins `archiver-client = { path = "../archiver/clients/python", editable = true }` — a **relative path dependency**. `uv sync` requires the repo at `../archiver` from this one, and honors no env var; moving it means editing that line.
- `tests/conftest.py` reads `ARCHIVER_REPO_PATH` (default `/home/exedev/archiver`) to locate Archiver's alembic for the cross-schema test tables. This is the **only** reader — CI sets it in `.github/workflows/ci.yml`.

So `ARCHIVER_REPO_PATH` redirects the test harness alone. Setting it without also fixing the path dependency yields passing tests over a broken `uv sync`.

The exe.dev proxy forwards 3000–9999. Dev server reachable at `https://watcher.exe.xyz:8001/`.

**Single process is load-bearing.** One uvicorn process runs everything: the API, the embedded Procrastinate worker, the `content.blobs` fact consumer, and the cache sweeper (started in the `src/api/main.py` lifespan — there is no separate worker unit). The reason is now the **fact consumer**, not politeness: `src/workers/fetch_facts.py` joins consumer group `watcher` as a single member (`watcher-1`), and a second process would need its own consumer name *and* an apply-ordering story across members — the supersession guard is per-row, not a cross-process lock. (Until #241 step 5 the reason was the in-process `DomainRateLimiter`; that retired with the local fetch path, so per-host pacing no longer constrains the topology at all — it is Replicator's, fed over `content.fetch-policy`.) Never run `uvicorn --workers N` or a second worker unit against prod. Escalation path when one process stops being enough (not before): a separate `watcher-worker.service` plus a multi-member consumer-group design — **not built**.

**Redis and the bus (archiver#109, #245).** Archiver operates `redis-server` on this VM — the tracked drop-in, persistence and version-floor policy, and producer-side monitoring are all its; it also owns the `info.changes` fact stream. **Watcher's Redis use is publish-only and exactly one thing**: the `content.fetch-policy` producer (#245; `src/core/fetch_policy.py` + the `publish_fetch_policy` periodic task) — Watcher's half of the cluster politeness split (*mechanism to Replicator, policy to the issuer, config over the bus*; normative: `docs/contracts/replicator-boundaries.md` in the Replicator repo). It publishes each `Domain.min_interval` (**never** `current_interval` — that column is inert 429-backoff state since the limiter retired) as a `FetchPolicyState` per host, full-set-republished every 5 minutes **including tombstones** (`fetch_policy_tombstones` table, written on domain delete, cleared on re-create) so a consumer's boot replay never depends on broker retention. Connection via `WATCHER_BUS_REDIS_URL` (unset → loud skip, Replicator falls back to its conservative default; `scripts/dev_server.sh` clears an inherited value unless `WATCHER_DEV_BUS_REDIS_URL` opts into a scratch bus). API domain routes defer an immediate republish; **dashboard routes deliberately don't** (they must not import `src.workers.*` — `tests/dashboard/test_import_decoupling.py`) and ride the periodic tick. Watcher **consumes nothing** and joins no consumer group; all async work stays on Procrastinate over Postgres (`PsycopgConnector`). Bus ownership design of record: `docs/plans/2026-07-29-redis-bus-ownership-design.md` in the Archiver repo ([on GitHub](https://github.com/CannObserv/archiver/blob/main/docs/plans/2026-07-29-redis-bus-ownership-design.md)).

**Phase 4 contracts (#241) — done.** Watcher **is** the `content.fetch` issuer and `content.blobs` consumer; it makes no origin request of its own on any scheduled path (cut over 2026-08-06; `WATCHER_FETCH_MODE` and the inline-fetch branch deleted in step 5). The `fetch_commands` outbox/inbox, the issue path in `check_watched_item`, the single-member `content.blobs` consumer, the apply tasks, and the reaper are all documented in **[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md)** — along with what step 5 retired, the inert `Domain` columns it left behind, and links to the two normative contracts in the Replicator repo (**link, don't copy**). Design: `docs/plans/2026-08-06-phase-4-content-fetch-producer-design.md`. #245 was the cutover's ordering blocker and shipped first.

**`info_source_id` on the wire (#252, cannobserv#300).** Every `content.fetch` Watcher publishes names the Archiver InfoSource it is for; **correlation is unchanged** — `command_id` only (MUST-3), and an unmatched fact is still discarded. **Deploy ordering is load-bearing:** Replicator must ship its echo (replicator#28) to production *first*, and the migration has no safe order. Both, plus why the field is reporting and not routing: **[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md)** and `docs/DEPLOYMENT.md` → "No safe order".

Other *future* work that would widen Redis use: a Redis-backed aspect-review cache (#163). It does not exist. *History:* the Watcher-side `info.changes` publisher (`src/core/changes/`, `ChangePublisher`) was deleted in **#156** (Phase 5 cutover); the producer role migrated to Archiver (archiver#106), and archiver#109 assigned operational ownership.

## Server Lifecycle

**Port 8000 belongs to systemd. Never start uvicorn manually on port 8000.**

After committing to `main`: `sudo systemctl restart watcher`. After DB model changes: `uv run alembic upgrade head` then restart. After Tailwind/vendor CSS changes: `bash scripts/build-css.sh` then restart. Logs: `sudo journalctl -u watcher -f`.

Dev server (port 8001, leaves prod alone):

```bash
bash scripts/dev_server.sh
```

Never launch uvicorn by hand with the prod env loaded — `/etc/watcher/.env`
points `DATABASE_URL` at production, and a hand-run "dev" server would share
the prod DB, run a second Procrastinate worker on the prod queue, and split
the rate-limiter budget (#233). The script targets `TEST_DATABASE_URL` (or
`WATCHER_DEV_DATABASE_URL`), migrates it, and refuses anything whose DB name
lacks a `_test`/`_dev` suffix. The same rule is enforced in-app by
`src/core/db_safety.py`; only `deploy/watcher.service` opts into prod via
`WATCHER_ALLOW_PRODUCTION_DB=1` (in the unit, never an env file).

**Archiver service.** Owns the canonical InfoItem / InfoSource / SourceRevision / RepSpec registry. Sibling repo (extracted in #149; see **Infrastructure** for checkout location). Watcher consumes it via the `archiver-client` SDK installed as a path dependency. Don't add Archiver code to this repo — go work in the sibling repo instead.

**Cross-repo policy.** Do not directly edit sibling repos (`archiver`, `notifier`) within a watcher conversation. If a change to a sibling is needed: identify the gap, recommend it, get approval, then file a GH issue in that repo. Implementation happens in a separate session scoped to the sibling.

Full lifecycle reference + cleanup timer: `docs/DEPLOYMENT.md`.

**No cross-repo mirror discipline (#159, #236).** Content acquisition is co-core's (see **Cannobserv wheelhouse** above); `src/core/logging.py` is service-local. Nothing in `src/` needs mirroring to Archiver — don't reintroduce a sync obligation.

## Environment Files

Two env files load in order (later overrides earlier):

1. `/etc/watcher/.env` — production secrets (`DATABASE_URL`, `NOTIFIER_API_KEY`, `ARCHIVER_API_KEY`). Persistent, managed manually on the VM.
2. `.env` (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`). Never commit.

Load both for shell commands (pytest, psql, gh):

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
```

Never follow this with a hand-run uvicorn — it leaves `DATABASE_URL` pointed
at production. The dev server is `bash scripts/dev_server.sh` (see **Server
Lifecycle**; #233).

**Naming rule for new variables.** Anything naming a shared external resource
takes a **service-prefixed** name with a separate dev key — Archiver's
`ARCHIVER_REDIS_URL` / `ARCHIVER_DEV_REDIS_URL` split is the pattern. A bare
unprefixed name (`REDIS_URL`) is silently inherited from `/etc/watcher/.env` by
anything that sources it, which is exactly how a dev process ends up pointed at
a production resource (the #233 hazard, in env-var form). Watcher's own
`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL` split (#245) follows the
pattern — see **Redis and the bus**.

**Key variables:**
- `DATABASE_URL` — PostgreSQL connection for watcher (Archiver owns its own database).
- `NOTIFIER_BASE_URL` — Notifier service URL for the `NotifierClient` SDK (e.g. `http://localhost:9000`). Required — every notification is dispatched through the notifier service.
- `NOTIFIER_API_KEY` — Required. Watcher tenant API key issued by `scripts/seed_tenant.py` in the notifier repo.
- `ARCHIVER_BASE_URL` — Archiver service URL for the `ArchiverClient` SDK (default: `http://localhost:8020`).
- `ARCHIVER_API_KEY` — Required. API key for the `ArchiverClient` SDK; missing key crashes the API on boot (pre-warm in lifespan).
- `WATCHER_CACHE_DIR` — Scratch directory for SourceRevision bytes (default `/var/cache/watcher/scratch`). Must be writable by the `watcher` user; create on fresh hosts.
- `WATCHER_CACHE_TTL_SECONDS` — Scratch-file lifetime before the sweeper removes it (default `600`).
- `WATCHER_CACHE_SWEEP_INTERVAL_SECONDS` — Sweeper periodic interval (default `60`).
- `WATCHER_BUS_REDIS_URL` — Broker URL for the `content.fetch-policy` producer (#245; prod: `redis://localhost:6379/0`). Unset → publish task skips loudly. Dev opts in via `WATCHER_DEV_BUS_REDIS_URL` (see **Redis and the bus**).

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

**Domain keying (#197).** `WatchedItem.domain_name` == `Domain.name` == `hostname(effective_url)` — the same string by construction (all derive from one `urlparse(...).hostname` over the same `effective_url`). That equality is what lets the fetch-policy producer publish per-`Domain.name` while items carry `domain_name`, and it is why `resolve_watch_target` derives the domain with the identical helper. **One entry per hostname** — host variants (`lcb.wa.gov` vs `www.lcb.wa.gov`) are independent by design. *History:* this used to describe the in-process `DomainRateLimiter`'s bucket key; the limiter retired with the local fetch path (#241 step 5) and per-host pacing is Replicator's, but the keying invariant still holds and is still load-bearing.

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
**[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md)**. On any PATCH that sets
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

Fresh hosts need the scratch directory: `sudo mkdir -p /var/cache/watcher/scratch && sudo chown watcher:watcher /var/cache/watcher/scratch` (or override via `WATCHER_CACHE_DIR`). The Archiver service must also be installed first — see its own `docs/DEPLOYMENT.md`. Archiver authoring tools (`validate_source_spec`, `fetch_and_render`, `preview_extraction`, `propose_selectors`, `find_info_item`, atomic `create_info_item`) are documented in the Archiver repo's `AGENTS.md`.

Operators manage WatchedItem defaults (`name`, `description`, `default_schedule_config`, `default_tags`; `content_media_type` is auto-detected but overridable — #168), archive/restore lifecycle, and notification-template CRUD via the `/watched-items` dashboard. Same surface is exposed at `/api/v1/watched-items`. WatchedItems are created **only** at `POST /api/v1/watched-items`, which requires `archiver_info_item_id` + `url` + `archiver_info_source_id` (#251 — the dashboard create form is gone; Archiver provisions via "Begin Watching"). Create and PATCH accept `is_active` (#188): create defaults `true`; pass `false` to provision paused. `is_active` is the **pause/resume** toggle (distinct from archive) — paused (`is_active=false`, not archived) items are skipped by `schedule_tick` and short-circuited by the `check_watched_item` task, but stay editable. The pause/resume rules live in one place (#228): `set_watched_item_active` (`src/core/watched_items.py`) owns the guards + the `WATCHED_ITEM_PAUSED`/`RESUMED` audit events, shared by the API PATCH and the dashboard toggle. PATCH `is_active` on an archived item is rejected (409 — restore first); activation while archived is owned by archive/restore; resume (`is_active=true`) while `domain_suspended` is rejected (409 — kill-switch parity with the dashboard toggle, unified in #228). Archive stamps `archived_at` and flips `is_active` (single entity — no child cascade since #191); restore clears `archived_at` and re-activates. **Permanent delete (#210):** `DELETE /api/v1/watched-items/{id}` → 204; **archived-only** (409 `"WatchedItem must be archived before deletion"` otherwise; 404 unknown/malformed). DB-level `ON DELETE CASCADE` removes the five children (`temporal_profiles`, item `notification_templates`, `change_revisions`, `pending_archiver_sync`, `fetch_commands`); a `WATCHED_ITEM_DELETED` audit row is written **before** the delete and survives it (id lives in the JSONB payload, no FK). Local-only — Archiver-side InfoItem/SourceRevisions are untouched. Deleting the last archived item on a domain frees the #209 domain delete guard. Filter by InfoItem with `GET /api/v1/watched-items?archiver_info_item_id=<ulid>`. Trigger an immediate check with `POST /api/v1/watched-items/{id}/check-now` (202). Its pre-flight mirrors **every** short-circuit in `check_watched_item`, so a request that could not do anything is rejected instead of returning 202 over a silent no-op: 409 archived, 409 paused, 409 `domain_suspended`, 409 a fetch command already open (the #241 one-command gate — post-cutover the likeliest; the message quotes the command's age and `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS` so an operator can tell a two-second wait from a stall), 422 empty `effective_url`.

**Dashboard parity (#190):** the dashboard surfaces pause/resume (`POST /watched-items/{id}/toggle-active` — mirrors the API 409 guards, blocks resume while `domain_suspended`, emits the `WATCHED_ITEM_PAUSED`/`RESUMED` events), check-now (`POST /watched-items/{id}/check-now` — delegates to the API route, so it inherits all five pre-flight guards; failures surface as an OOB flash carrying the API's detail text), effective_url editing (`POST /watched-items/{id}/effective-url` — re-probes to re-derive `domain_name`, leaves `source_specs` untouched), and permanent delete (`POST /watched-items/{id}/delete` — delegates to the API DELETE; the **Delete permanently** control lives in the archived branch of the detail Danger Zone only, redirects to `/watched-items` on success, surfaces the 409 as an OOB flash; #210). Pause/resume + check-now controls appear on the WatchedItem detail page and in the list rows. `source_specs` is shown read-only on detail (authoring stays in Archiver tooling). The detail page surfaces a single item-template panel (item-scoped `NotificationTemplate` CRUD) plus read-only Global/Domain inherited sections; the full API surface lives at `/api/v1/watched-items/{id}/notifications` (see **Notifications**).

**Watched Items list view** (`#172`, `#173`, `#190`): columns are Name → Last Check → Interval → Next Check → Status → Actions (per-row pause/resume toggle + check-now). The Status badge distinguishes Active / Paused / Domain Inactive / Archived. Interval and Next Check resolve through `resolve_schedule_display` (the #206 helper over the 3-tier `resolved_schedule_config`; #204, #205, #206), so an item with no explicit `default_schedule_config` shows the inherited interval with a source marker (`· domain` when inherited from the Domain cadence, `· default` from the system default, `· profile` when an active `TemporalProfile` overrides the base cadence) plus a computed Next Check rather than blanks — parity with the detail page and consistent with `schedule_tick`. Next Check is a live countdown rendered by `src/dashboard/static/js/next-check-countdown.js` (loaded globally via `base.html`; reads `data-next-check` ISO timestamp attributes, refreshes every 60 s). List has server-side name search and pagination: `GET /partials/watched-items-table?q=&page=&page_size=&include_archived=` is the HTMX partial; the full page (`GET /watched-items`) accepts the same params and SSR-includes the partial on first load. Active/All archived toggle is a segment-group that cross-includes the search input. Aspect Review column removed (#173) — too expensive per-row; will surface on WatchedItem detail page behind a Redis cache (tracked in #163).

**Domain WatchedItem counts (#209):** the Domains-list "Watched Items" column and the domain-detail heading count *live* (non-archived) items only, and the list's "Last Checked" `max` excludes archived items too. The detail heading shows the archived remainder explicitly (`N · M archived`). The domain **delete guard** is the one place that counts the archived-inclusive total — archived WatchedItems still hold the `domain_name` FK, so deletion stays blocked while any reference exists.

**InfoItem picker removed** (`#185 Phase A step 7`): the InfoItem typeahead picker (routes `GET /info-items/search`, `GET /info-items/{id}/binding-tree`; JS `info-item-picker.js`; templates `partials/info_item_picker/`) was removed. Nothing replaced it: WatchedItem-create is API-only and InfoItem-linked (#251), and no create path probes (#241) — `effective_url` comes from Archiver and `domain_name` is derived from it (the separate Watch-create flow no longer exists — #191).

**Watched Item detail** (`#174`, updated `#185`, `#190`, `#191`, `#199`, `#202`, `#215`): the heading carries a subdued **Watched Item** eyebrow. A single **Details** panel holds, in order, Name, the `effective_url` row (inline **Edit** — re-derives the domain without probing, #241), Domain, the Status pause/resume toggle (the sole status badge), `last_checked_at` (with the Check-now action and the **Health** badge inline — health is the result of that check, surfaced with an accessible hover/focus tooltip), `last_changed_at`, Interval (when unset, shows the inherited cadence with a `· domain`/`· default` source marker, or `· profile` when an active temporal profile overrides it; #205, #206), Content Type, Description, and Tags — all from local WatchedItem columns, no Archiver SDK calls. Below it: a read-only `source_specs` panel, then the **Notification Templates** panel (item-scoped CRUD plus read-only **Global**/**Domain** inherited sections that fire at dispatch — parity with the Domain detail page; #199, unified in #200), then **Recent Activity**. `POST /watched-items/{id}/mark-reviewed` (stamps `last_reviewed_at`) remains API-only — the dashboard route exists but is intentionally unwired; no dashboard UI until a replacement is designed.

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
sibling repo elsewhere — but note this only redirects **this** alembic
invocation. The `archiver-client` path dependency is separately pinned to
`../archiver/clients/python` in `[tool.uv.sources]` and ignores the variable;
relocating the checkout means editing that too (see **Infrastructure**).

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

### CI (#220)

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`: a
**lint** job (`ruff check` + `ruff format --check`), a **test** job
(`pytest -m "not integration"` against a `postgres:16` service), and a
**migrations** job (independent migration-chain smoke-check, #234 — `alembic
upgrade head` from an empty `postgres:16` then `alembic check` for drift). All
three jobs checkout the sibling `archiver` repo alongside watcher (public;
resolves the `archiver-client` path dep + provides conftest's alembic), rewrite
the `notifier-client` SSH source to HTTPS, authenticate to GCS **keyless via
WIF** (`vars.GCP_WIF_PROVIDER` → `co-pypi-reader` SA), and sync the wheelhouse
before `uv sync`. Only the test job also syncs archiver's wheelhouse (its `uv
run alembic` subprocess needs co-core); the migrations job does **not** — the
#234 squash collapsed the pre-existing chain into a self-contained genesis
baseline (`2addddea0b03`) that references no `information` schema, so `upgrade
head` from empty is fully standalone (no archiver seeding, no cross-service
ordering). **Squash cutover:** already-migrated DBs need a one-time `alembic
stamp 2addddea0b03 --purge` before their next upgrade — see `docs/DEPLOYMENT.md`
→ "Migration baseline (squash)". Integration tests hit live external services
and are excluded in CI. **One-time GCP grant** (operator, for WIF) — bind watcher's repo
to the read-only SA; the org-scoped `github-ci` provider needs no change:
```bash
gcloud iam service-accounts add-iam-policy-binding \
  co-pypi-reader@co-gcs.iam.gserviceaccount.com --project=co-gcs \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/912903030445/locations/global/workloadIdentityPools/github/attribute.repository/CannObserv/watcher"
```

## Agent Skills

Skills live in `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Local overrides in `skills/` shadow vendor submodules in `skills-vendor/`.

| Skill | Triggers / when to invoke |
|---|---|
| `reviewing-code-python-fastapi` | CR, code review |
| `reviewing-architecture` | AR, architecture review |
| `enforcing-architecture` | add a fitness function, enforce this contract, lock this rule (delegated to by `reviewing-architecture` on a `fitness` directive) |
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

Every record serializes as JSON with **at least** four keys — `timestamp` (ISO 8601
UTC), `level`, `logger`, `message` — plus `exc_info` when logging an exception and
any extras the emitting library attaches (procrastinate adds `action`/`job`/…, and
uvicorn's own lines carry extras of their own). Those four are
the contract — a floor, not an exhaustive list (the set matches structlog's defaults,
so a future structlog/OTel migration won't churn log consumers) — and are pinned by
`tests/core/test_logging.py` (#238); don't rename or drop keys without updating both.

**uvicorn's own loggers need `--log-config` (#244).** `uvicorn`, `uvicorn.access`,
and `uvicorn.error` ship with `propagate=False` and their own plain-text handlers,
so `configure_logging()` — which touches only the **root** logger — never reaches
them; without the flag journald gets mixed formats (plain access lines interleaved
with JSON app records). Every uvicorn invocation therefore passes
`--log-config src/core/log_config.json` (already wired into
`deploy/watcher.service` and `scripts/dev_server.sh` — the only two sanctioned
launch paths). That dictConfig file carries **no** copy of the format string: its
`"()"` key calls `build_json_formatter()` in `src/core/logging.py`, the single
formatter definition shared with `configure_logging()`. Both facts are pinned by
`tests/core/test_logging.py`.

Each of the three uvicorn loggers also lists the `strip_color_message` **filter**
(`ColorMessageFilter`, `src/core/logging.py`) — uvicorn attaches an ANSI-coloured
duplicate of every lifecycle line as `extra={"color_message": ...}`, and extras
reach the JSON payload (#246). It sits on the *loggers*, not the stdout handler
and not the formatter's `reserved_attrs`: those clean one sink only, so a handler
that serializes `record.__dict__` directly (OTel's `LoggingHandler`, whose
reserved list omits `color_message`) would silently resurrect the field. Listing
it on all three is load-bearing — propagation walks ancestors' *handlers*, never
their filters, so a filter on the parent `uvicorn` alone never sees a
`uvicorn.error` record.

**One exception — `ExecStartPre` output is plain text (#247).** The JSON claim
above scopes to the *application's* records. `deploy/watcher.service` runs the
wheelhouse sync as a non-fatal `ExecStartPre`, so journald also gets a plain-text
line on every service start (`wheelhouse in sync: N downloaded, M already present
-> …`, or `error: could not sync gs://…` on the failure path — the one that
appears exactly when something is already wrong). That is by design, not drift:
the step runs under `uv run --no-project` *before* `uv sync`, cannot import the
project, and so cannot share `build_json_formatter()`; emitting JSON would mean a
second hand-maintained copy of the key schema. The unit's other two `ExecStartPre`
lines are silent on success but plain-text on failure the same way (`git rev-parse`
writes `fatal: not a git repository` to stderr; only stdout is redirected to the
build-id file). A log pipeline that `json.loads` every journald `MESSAGE` must
tolerate all of them (reading the entry's native fields — `_SYSTEMD_UNIT`,
`SYSLOG_IDENTIFIER`, `MESSAGE` — is unaffected); the `jq` follow-recipe in
`docs/DEPLOYMENT.md` is written to survive them — bare `jq` aborts on the first
plain line and hides every record after it.

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
