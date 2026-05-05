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
src/information/ Information service — sibling FastAPI app on port 8020; InfoItem + InfoSpec registry (own alembic root: alembic_information/)
clients/python/  Python SDK for the Information service (information-client); consumed by Watcher (Phase 2c) and Archive (Phase 3+)
tools/           Operational scripts (e.g. info_changes_consumer.py — reference XREADGROUP consumer)
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
| Information service | 8020 | `systemctl` (`information.service`) |

The exe.dev proxy forwards 3000–9999. Dev server reachable at `https://watcher.exe.xyz:8001/`.

## Server Lifecycle

**Port 8000 belongs to systemd. Never start uvicorn manually on port 8000.**

After committing to `main`: `sudo systemctl restart watcher`. After DB model changes: `uv run alembic upgrade head` then restart. After Tailwind/vendor CSS changes: `bash scripts/build-css.sh` then restart. Logs: `sudo journalctl -u watcher -f`.

Dev server (port 8001, leaves prod alone):

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

**Information service.** Owns the canonical Information Item + InfoSpec registry. Lives at `src/information/`. Runs as `information.service` on port 8020 once installed. Migrations: `uv run alembic -c alembic_information.ini upgrade head`. Dev server: `uv run uvicorn src.information.api.main:app --host 0.0.0.0 --port 8021 --reload`.

Full lifecycle reference + cleanup timer: `docs/DEPLOYMENT.md`.

## Environment Files

Two env files load in order (later overrides earlier):

1. `/etc/watcher/.env` — production secrets (`DATABASE_URL`, `APPRISE_SECRET_KEY`, `REDIS_URL`). Persistent, managed manually on the VM.
2. `.env` (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`, `REDIS_URL`). Never commit.

Load both for shell commands:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
```

**Key variables:**
- `DATABASE_URL` — PostgreSQL connection (watcher + information service)
- `APPRISE_SECRET_KEY` — HMAC signing key for Apprise webhook validation
- `REDIS_URL` — Redis connection URL (default: `redis://localhost:6379/0`). Used by `ChangePublisher` and `tools/info_changes_consumer.py`. Override for testing or remote Redis.
- `INFORMATION_BASE_URL` — Information service URL for the `InformationClient` SDK (default: `http://localhost:8020`).
- `INFORMATION_API_KEY` — Required. API key for the `InformationClient` SDK; missing key crashes the API on boot (pre-warm in lifespan).

Full variable reference: `docs/DEPLOYMENT.md`.

## Watches & change bus (Phase 2c)

Watches are InfoItem-native: a Watch references an `info_item_id` and resolves URL + fetch defaults from the primary `InfoSpec` at every callsite (`check_watch`, screenshot capture, dashboard preview). Watch creation requires `info_item_id` — the Information service is the source of truth for the URL. Legacy `url` and `fetch_config` columns no longer exist.

Change bus envelope is `schema_version: 2`. Stream entries are partitioned by `info_item_id` (Phase 2b's v1 partitioned by `watch_id`) and carry `info_item_id`, `info_spec_id`, plus `previous_fingerprint`/`current_fingerprint`. The `drain_changes_outbox` task is registered as `@bp.periodic(cron="* * * * *")`, so the embedded worker drains every minute. A PostgreSQL transaction-scoped advisory lock (`DRAIN_ADVISORY_LOCK_ID`) keeps concurrent drains from double-publishing.

Fresh hosts need `sudo cp deploy/information.service /etc/systemd/system/` before `watcher.service` will boot — see `docs/DEPLOYMENT.md` for the full install (key generation + env-var registration). On hosts where the unit isn't installed, the dev server `uv run uvicorn src.information.api.main:app --host 0.0.0.0 --port 8021 --reload &` is acceptable for development; set `INFORMATION_BASE_URL=http://localhost:8021` so consumers (Watcher dev server, smoke scripts) hit it instead of the systemd port.

## Information service authoring tools (Phase 3a)

The Information service exposes authoring helpers under `/api/v1/tools/*`. Non-mutating except where noted; same `X-API-Key` auth as the CRUD surface. Each route has an ergonomic SDK wrapper on `InformationClient`.

| Tool | HTTP | SDK method | Use when |
|---|---|---|---|
| `validate_info_spec` | `POST /tools/validate-info-spec` | `validate_info_spec(doc)` | Surface schema problems on a candidate doc before `create_info_spec`. |
| `find_info_item` | `GET /tools/find-info-items?q=…` | `find_info_item(query, limit=20)` | Dedupe before creating a new InfoItem. Substring + case-insensitive over name + description. |
| `fetch_and_render` | `POST /tools/fetch-and-render` | `fetch_and_render(url)` | Inspect what the extractor will see. v1 is HTTP-only; `render=True` returns 501 until #3 (Playwright). 5 MiB body cap. |
| `preview_extraction` | `POST /tools/preview-extraction` | `preview_extraction(url, doc)` | Dry-run validate + fetch + extract + fingerprint. 422 with structured `validation_failed` / `target_unreachable` codes. |
| `propose_selectors` | `POST /tools/propose-selectors` | `propose_selectors(url, description, top_k=5)` | Rank CSS selector candidates; heuristic v1 (#146 tracks learned ranker). |
| `create_info_item` (atomic) | `POST /info-items` w/ `initial_info_spec` | `create_info_item(..., initial_info_spec=doc)` | Mutating. Atomically create the InfoItem + primary InfoSpec in one transaction. |

Smoke: `bash scripts/smoke_phase3a.sh` exercises the full authoring loop end-to-end against the live service.

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

## Agent Skills

Skills live in `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Local overrides in `skills/` shadow vendor submodules in `skills-vendor/`.

| Skill | Triggers / when to invoke |
|---|---|
| `reviewing-code-claude` | CR, code review |
| `reviewing-architecture-claude` | AR, architecture review |
| `shipping-work-claude` | ship it, push GH, close GH, wrap up |
| `brainstorming` | brainstorm, design this, let's design |
| `writing-plans` | write plan, implementation plan |
| `writing-skills` | write skill, new skill, author skill |
| `systematic-debugging` | any bug, test failure, unexpected behavior |
| `verification-before-completion` | before any completion claim or commit |
| `test-driven-development` | before writing implementation code |
| `subagent-driven-development` | dispatch agents for plan execution |
| `dispatching-parallel-agents` | 2+ independent tasks in parallel |
| `using-git-worktrees` | feature work needing isolation |
| `managing-skills-claude` | add skill repo, manage external skills |
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

**DB Triggers (gotcha):**
- Triggers live in Alembic migrations (`CREATE OR REPLACE FUNCTION` + `CREATE OR REPLACE TRIGGER`; downgrade with `DROP TRIGGER IF EXISTS … ON table; DROP FUNCTION IF EXISTS …`).
- Integration tests use `Base.metadata.create_all` (not migrations), so triggers are NOT applied automatically. Any trigger added in a migration must also be recreated in `tests/conftest.py` inside the `test_engine` fixture, after `create_all`.
- Current triggers: `trg_changes_update_last_changed_at` (AFTER INSERT ON changes → sets `watches.last_changed_at = NEW.detected_at`).

## Style & UI

Authoritative reference: `docs/STYLE.md`.

**Brand:** Cannabis Observer — `co-purple-600` (#6d4488) primary accent. Never use brand colors for semantic status (green/yellow/red/blue).

**Dark Mode:** Tailwind `dark:` variants on every color utility. Class-based toggle (`<html class="dark">`). localStorage key: `watcher-color-scheme`.

**Accessibility:** WCAG 2.1 AA. Skip link, ARIA landmarks, `focus-visible` rings, 44px touch targets, `aria-live` on HTMX swap targets, reduced motion. Wrap decorative emoji in `<span aria-hidden="true">`. No `title` attributes.

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.segment-group`, `.segment`, `.chip-group`, `.chip`, `.detail-grid`, `.toggle`, `.danger-zone`). Badge variants: `.badge-active` (green), `.badge-inactive` (gray), `.badge-archived` (amber), `.badge-error` (red), `.badge-warning` (orange), `.badge-info` (blue). Use CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. Detect HTMX via `HX-Request` header with `HX-Boosted` guard. All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.
