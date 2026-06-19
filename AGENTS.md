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
used by the pipeline; `default_schedule_config`, `default_content_type`,
`default_tags`; `domain_name` (FK → `Domain.name`, set at create time);
`domain_suspended` (set True/False by domain deactivation/reactivation — it
gates scheduling directly, no live Domain join); a single optional
`TemporalProfile` (1:1, `temporal_profiles.watched_item_id`); `health_status`,
`last_checked_at`, `last_changed_at`; and its notification surface
(`WatchedItemNotificationTemplate` rows + `watch_notification_configs` /
`watch_nc_refs`, both re-keyed to `watched_item_id`). Schedule resolution is
just WatchedItem default → system default (`src/core/watches/resolution.py`).

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

**WatchEvent identity fields** are `watched_item_id`, `item_name`, `item_url`
(renamed from `watch_*` in #191). The same names are the user-facing notification
template variables; the default-template "WATCH:" link and `change_url` point at
`/watched-items/{watched_item_id}`. The `AuditLog.watch_id` FK column was retired —
audits carry the WatchedItem as `watched_item_id` inside the JSONB `payload`
(filter via `GET /api/v1/audit?watched_item_id=<ulid>`).

Fresh hosts need the scratch directory: `sudo mkdir -p /var/cache/watcher/scratch && sudo chown watcher:watcher /var/cache/watcher/scratch` (or override via `WATCHER_CACHE_DIR`). The Archiver service must also be installed first — see its own `docs/DEPLOYMENT.md`. Archiver authoring tools (`validate_source_spec`, `fetch_and_render`, `preview_extraction`, `propose_selectors`, `find_info_item`, atomic `create_info_item`) are documented in `/home/exedev/archiver/AGENTS.md`.

Operators manage WatchedItem defaults (`name`, `description`, `default_schedule_config`, `default_content_type`, `default_tags`), archive/restore lifecycle, and notification-template CRUD via the `/watched-items` dashboard. Same surface is exposed at `/api/v1/watched-items`. WatchedItems are created at `POST /api/v1/watched-items` (accepts `archiver_info_item_id` or `url`; both optional but at least one required) or `GET/POST /watched-items/new` (dashboard — URL-first; an `is_active` checkbox provisions paused). Create and PATCH accept `is_active` (#188): create defaults `true`; pass `false` to provision paused. `is_active` is the **pause/resume** toggle (distinct from archive) — paused (`is_active=false`, not archived) items are skipped by `schedule_tick` and short-circuited by the `check_watched_item` task, but stay editable. PATCH `is_active` on an archived item is rejected (409 — restore first); activation while archived is owned by archive/restore. Archive stamps `archived_at` and flips `is_active` (single entity — no child cascade since #191); restore clears `archived_at` and re-activates. Filter by InfoItem with `GET /api/v1/watched-items?archiver_info_item_id=<ulid>`. Trigger an immediate check with `POST /api/v1/watched-items/{id}/check-now` (202; pre-flight: not archived, not paused, has `effective_url`).

**Dashboard parity (#190):** the dashboard surfaces pause/resume (`POST /watched-items/{id}/toggle-active` — mirrors the API 409 guards, blocks resume while `domain_suspended`, emits the `WATCHED_ITEM_PAUSED`/`RESUMED` events), check-now (`POST /watched-items/{id}/check-now` — delegates to the API route, guard failures flash), and effective_url editing (`POST /watched-items/{id}/effective-url` — re-probes to re-derive `domain_name`, leaves `source_specs` untouched). Pause/resume + check-now controls appear on the WatchedItem detail page and in the list rows. `source_specs` is shown read-only on detail (authoring stays in Archiver tooling). The detail page surfaces a notification-configs panel and a WatchedItem-template panel (the per-Watch tier was folded into the WatchedItem in #191; the template panel also surfaces read-only Global/Domain inherited sections since #199; richer notification-config CRUD on the dashboard is a follow-up — the full surface lives at `/api/v1/watched-items/{id}/notifications`).

**Watched Items list view** (`#172`, `#173`, `#190`): columns are Name → Last Check → Interval → Next Check → Status → Actions (per-row pause/resume toggle + check-now). The Status badge distinguishes Active / Paused / Domain Inactive / Archived. Next Check is a live countdown rendered by `src/dashboard/static/js/next-check-countdown.js` (loaded globally via `base.html`; reads `data-next-check` ISO timestamp attributes, refreshes every 60 s). List has server-side name search and pagination: `GET /partials/watched-items-table?q=&page=&page_size=&include_archived=` is the HTMX partial; the full page (`GET /watched-items`) accepts the same params and SSR-includes the partial on first load. Active/All archived toggle is a segment-group that cross-includes the search input. Aspect Review column removed (#173) — too expensive per-row; will surface on WatchedItem detail page behind a Redis cache (tracked in #163).

**InfoItem picker removed** (`#185 Phase A step 7`): the InfoItem typeahead picker (routes `GET /info-items/search`, `GET /info-items/{id}/binding-tree`; JS `info-item-picker.js`; templates `partials/info_item_picker/`) was removed. WatchedItem-create accepts a URL directly and probes it for `effective_url` + `domain_name` (the separate Watch-create flow no longer exists — #191).

**Watched Item detail** (`#174`, updated `#185`, `#190`, `#191`, `#199`): shows `effective_url` (with a re-probe edit affordance), `last_checked_at`, `last_changed_at`, and `health_status` (shown even when UNKNOWN) from local WatchedItem columns — no Archiver SDK calls. Includes a Status pause/resume toggle, a Check-now button, a read-only `source_specs` panel, a notification-configs panel, and a notification-template panel — the latter also surfaces read-only **Global** and **Domain** sections listing inherited templates that fire at dispatch (parity with the Domain detail page; #199). `POST /watched-items/{id}/mark-reviewed` (stamps `last_reviewed_at`) remains API-only — the dashboard route exists but is intentionally unwired; no dashboard UI until a replacement is designed.

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

**Accessibility:** WCAG 2.1 AA. Skip link, ARIA landmarks, `focus-visible` rings, 44px touch targets, `aria-live` on HTMX swap targets, reduced motion. Wrap decorative emoji in `<span aria-hidden="true">`. No `title` attributes.

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.segment-group`, `.segment`, `.chip-group`, `.chip`, `.detail-grid`, `.toggle`, `.danger-zone`). Badge variants: `.badge-active` (green), `.badge-inactive` (gray), `.badge-archived` (amber), `.badge-error` (red), `.badge-warning` (orange), `.badge-info` (blue). Use CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. Detect HTMX via `HX-Request` header with `HX-Boosted` guard. All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.
