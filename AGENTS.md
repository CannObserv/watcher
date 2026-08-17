# watcher — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Sacrifice grammar for density. Lead with the answer or action.

## Project Overview

Web service for monitoring cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff; Node.js + npm (for Tailwind CLI — `sudo npm install -g @tailwindcss/cli`, one-time VM setup).

**Cannobserv wheelhouse.** Populate it before any `uv` command — `[tool.uv]
find-links` makes every invocation require the directory:

```bash
uv run --no-project --with 'google-cloud-storage>=2,<4' python scripts/sync_wheelhouse.py
uv sync
```

Why a wheelhouse and not git sources, the ADC/WIF auth, the upgrade procedure, and the
pinned version: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) → *Cannobserv wheelhouse*.
`co-core` owns fetch → extract → fingerprint; watcher no longer fetches at all —
[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md).

## Code Exploration Policy

SocratiCode is indexed on this repo (`.socraticodecontextartifacts.json` present). Its MCP tools are **deferred** — schemas load only after a `ToolSearch` prefetch. The SessionStart hook prints the prefetch query; run it before exploring.

**Negative rule.** For broad semantic questions ("where is X", "how does Y work", "what depends on Z"), use SocratiCode MCP tools first. Reach for `grep`/`ripgrep` only on exact strings (error messages, log lines, known symbols). Reserve the Explore subagent for path-pattern walks (e.g. "all `*.py` under `src/api/routes/`"), not semantic search.

[docs/SKILLS.md](docs/SKILLS.md) has the rest: *When to use each tool* (the goal→tool table), *Index scope* (`.socraticodeignore`, #240) and its rebuild procedure, and *Prefetch query* — the literal query, if the hook's reminder didn't load.

## Infrastructure

**Single-VM setup.** Dev and prod on the same VM. Code committed to `main` is the deployed code. Systemd service `watcher` runs the live site on port 8000.

| Service | Port | Managed by |
|---|---|---|
| API (live) | 8000 | `systemctl` (`watcher.service`) |
| API (dev) | 8001 | manual uvicorn |
| Archiver | 8020 | `systemctl` (`archiver.service`) |

`ARCHIVER_REPO_PATH` redirects everything needing the sibling repo (#254): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

The exe.dev proxy forwards 3000–9999. Dev server reachable at `https://watcher.exe.xyz:8001/`.

**Single process is load-bearing.** One uvicorn process runs everything — API, embedded Procrastinate worker, `content.blobs` fact consumer, cache sweeper. **Never run `uvicorn --workers N` or a second worker unit against prod.** Why the fact consumer makes this load-bearing, and the escalation path that is *not built*: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Single process*.

**The bus.** Watcher publishes `content.fetch`, `content.fetch-policy`, `content.revisions`, and `info.watch-status`; consumes `content.blobs` (single-member group `watcher`) and `info.registry` (**groupless**, replayed from `0-0` every boot). Archiver operates the broker. `WATCHER_BUS_REDIS_URL` unset → publish tasks skip loudly. Stream ownership, the fetch contracts, and `info_source_id` on the wire:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Server Lifecycle

**Port 8000 belongs to systemd. Never start uvicorn manually on port 8000.**

After committing to `main`: `sudo systemctl restart watcher`. After DB model changes: `uv run alembic upgrade head` then restart. After Tailwind/vendor CSS changes: `bash scripts/build-css.sh` then restart. Logs: `sudo journalctl -u watcher -f`.

Dev server (port 8001, leaves prod alone):

```bash
bash scripts/dev_server.sh
```

**Never launch uvicorn by hand with the prod env loaded** — it would share the prod DB and run a second worker on the prod queue (#233). The script refuses any DB whose name lacks a `_test`/`_dev` suffix; `src/core/db_safety.py` enforces the same in-app. Full rationale: [docs/COMMANDS.md](docs/COMMANDS.md) → *Development*.

**Archiver owns the canonical registry**; watcher consumes it over the bus and makes **no HTTP calls to Archiver at all** — re-adding an SDK is a design regression. Don't add Archiver code to this repo: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Sibling services*.

**Cross-repo policy.** Do not directly edit sibling repos (`archiver`, `notifier`) within a watcher conversation. If a change to a sibling is needed: identify the gap, recommend it, get approval, then file a GH issue in that repo. Implementation happens in a separate session scoped to the sibling.

Full lifecycle reference + cleanup timer: `docs/DEPLOYMENT.md`.

**Nothing in `src/` mirrors to Archiver** (#159, #236) — don't reintroduce a sync obligation: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *No cross-repo mirror discipline*.

## Environment Files

Two env files load in order (later overrides earlier):

1. `/etc/watcher/.env` — production secrets (`DATABASE_URL`, `NOTIFIER_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`). Persistent, managed manually on the VM.
2. `.env` (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`). Never commit.

Load both for shell commands (pytest, psql, gh):

```bash
source scripts/load-env.sh
```

Never follow this with a hand-run uvicorn — it leaves `DATABASE_URL` pointed
at production. The dev server is `bash scripts/dev_server.sh` (see **Server
Lifecycle**; #233).

**Naming rule for new variables.** Anything naming a shared external resource takes a **service-prefixed** name with a separate dev key (`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL`). A bare `REDIS_URL` is silently inherited from `/etc/watcher/.env` — the #233 hazard in env-var form. Rationale: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) → *Environment Variables*.

Full variable reference: `docs/DEPLOYMENT.md`.

## Common Commands

```bash
uv sync                                      # install deps
uv run pytest                                # tests
uv run pytest -m integration                 # integration tests (needs PostgreSQL)
uv run ruff check .                          # lint
uv run alembic upgrade head                  # apply migrations
```

**Never run `alembic revision --autogenerate` against `DATABASE_URL`** — it diffs
the models against whatever it connects to, which is production. Build a scratch
database first (that is what `CREATEDB` on the migration role is for, #259):
[docs/COMMANDS.md](docs/COMMANDS.md) → *Autogenerate wants a scratch database*.
Alembic connects with `WATCHER_MIGRATION_DATABASE_URL`, else `DATABASE_URL`;
`alembic.ini` carries no URL, so an unloaded shell fails instead of defaulting to
production.

Full reference: `docs/COMMANDS.md`.

## Watched Items

**The `WatchedItem` is the single monitored entity (#191).** The earlier
`WatchedItem → Watch` one-to-many was collapsed: `Watch` (the model, table,
`/watches*` routes, the override resolution chain, and the per-Watch
notification tier) is gone. One `WatchedItem` = one URL = one fingerprint = one
change signal. The user-facing noun is "Watched Item".

**The `info.registry` reconcile is the creation path** — it creates from an announcement alone, so a cold start converges from the snapshot. `POST /api/v1/watched-items` still exists and still works, but Archiver retired its outbound provisioning call in archiver#158 (2026-08-17), so it currently has no caller. `source_specs` is required and non-empty there (#260); the reconcile is not gated, so a spec-less item stays reachable over the wire and raises `ExtractionError` at pipeline time rather than silently watching the whole page. `WatchedItem.domain_name` == `Domain.name` == `hostname(effective_url)`; one entry per hostname, so host variants are independent by design.

**The registry owns cadence and active state; Watcher owns mechanism (#254).** An announcement is authoritative for exactly five columns (`archiver_info_source_id`, `effective_url`, `source_specs`, `announced_schedule_config`, `is_active`) plus `domain_name`; everything else — health, timings, `domain_suspended`, `archived_at`, `throttle_floor_interval`, tags, notification config — survives reconciliation. **A local pause is not sticky**: item-level pause lives in Archiver's dashboard alone, and on a reconciled item every announcement-owned field 409s locally. Schedule resolution is four tiers under a floor. The full rules — what each 409 is, what restore does and doesn't do, how the floor is released: [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md) → *Registry reconciliation*.

**Empty extraction is a failure, not a change (#258).** When every `source_spec`
yields empty chunks, `process_watched_item` raises `ExtractionError` and writes
nothing — unconditionally, on both sides of a baseline. Before the guard,
selector rot presented as a *content change* with health still OK. Full
rationale, and the six provenance columns the outbox gained for
`source_revision_observed` (#253): **[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md)**.

**Notifications.** One `notification_templates` table; a row's `visibility` —
`global` / `domain` / `watched_item` — is what decides where it fires.

**Notification bodies are source Markdown and must be block-structured**, never
`\n`-joined — guarded by
`tests/core/notifications/test_content.py::TestMarkdownListContract`.

Fields, 3-tier schedule resolution, media-type dispatch and template CRUD: [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md). Lifecycle and delete guards, and every dashboard surface: [docs/WATCHED-ITEMS-DASHBOARD.md](docs/WATCHED-ITEMS-DASHBOARD.md).

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

Records are JSON with a four-key floor — `timestamp`/`level`/`logger`/`message` — pinned by `tests/core/test_logging.py`;
don't rename or drop a key without updating both. Why that set, and the rest of the
logging configuration: [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

uvicorn's own loggers need `--log-config src/core/log_config.json` (both sanctioned launch paths already pass it) plus the `strip_color_message` filter.
[docs/CONVENTIONS.md](docs/CONVENTIONS.md).

**Date & Time:** All UTC. ISO 8601: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (timestamps), `YYYY-MM-DD` (dates).

**General:**
- No inline module imports; all at file top
- Docstrings for public modules, classes, functions
- Test structure mirrors source (`src/foo.py` → `tests/test_foo.py`)
- Explicit imports only
- Small, focused functions
- Optional JSONB columns: declare as `JSONB(none_as_null=True)` so Python `None` persists as SQL `NULL`, not a JSONB `'null'` literal (otherwise `WHERE col IS NULL` silently misses those rows — #198)

**ULID format errors:** path parameter → 404 (`parse_ulid`), filter query parameter → 400
(`parse_filter_ulid`). **DB triggers:** currently none; any trigger added in a migration
must also be recreated in `tests/conftest.py`'s `test_engine` fixture, because integration
tests build the schema with `create_all` rather than migrations. Both:
[docs/CONVENTIONS.md](docs/CONVENTIONS.md).

## Style & UI

Authoritative reference: `docs/STYLE.md`.
Component classes and the HTMX/flash patterns: [docs/UI.md](docs/UI.md).

**Brand:** Cannabis Observer — `co-purple-600` (#6d4488) primary accent. Never use brand colors for semantic status (green/yellow/red/blue).

**Dark Mode:** Tailwind `dark:` variants on every color utility. Class-based toggle (`<html class="dark">`). localStorage key: `watcher-color-scheme`.

**Accessibility:** WCAG 2.1 AA. **Touch-target idiom (#203):** component classes own the 44px guarantee — never restate `min-h-[44px]` on a `.btn`, never `min-h-0`. Skip links, ARIA landmarks, `focus-visible`, `aria-live`, reduced motion, no `title` attributes: [docs/STYLE.md](docs/STYLE.md) §7–8 (guards: `tests/dashboard/test_touch_targets.py`, `scripts/check-touch-targets.sh`).

**CSS:** Tailwind v4 with `@theme` in `input.css`; use the component classes rather
than raw utilities. Full class inventory and badge variants:
[docs/UI.md](docs/UI.md).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. **Detect HTMX with `is_htmx(request)`** ([src/dashboard/deps.py](src/dashboard/deps.py)), never a bare `HX-Request` read — guarded by `tests/dashboard/test_htmx_detection.py` (#211). Patterns: [docs/UI.md](docs/UI.md) → *HTMX Patterns*.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.

## Agent Skills

Skills live in `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Local overrides in `skills/` shadow vendor submodules in `skills-vendor/`.

Full skill reference: `docs/SKILLS.md`. Cross-project search to the sister `notifier` index requires a per-instance `.claude/settings.local.json` (gitignored) — see "Linked Projects" in `docs/SKILLS.md`.

## Detail Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module layout, sibling-service topology, the Archiver checkout constraint, and the Redis bus topology, streams, and fetch contracts
- [docs/COMMANDS.md](docs/COMMANDS.md) — every runnable command, the Archiver-sibling test setup, and CI
- [docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md) — fetch → extract → fingerprint, the fetch-command outbox, the revisions producer
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — logging configuration, ULID error handling, DB-trigger rules
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — systemd units, environment variables, migration ordering, wheelhouse auth
- [docs/SKILLS.md](docs/SKILLS.md) — skill triggers, vendored skill repos, SocratiCode workflow
- [docs/STYLE.md](docs/STYLE.md) — the design system: brand, color, dark mode, tokens, layout, touch targets, accessibility
- [docs/UI.md](docs/UI.md) — the component library (`.btn`, `.badge`, `.data-table`, …) and the HTMX/flash interaction patterns
- [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md) — the WatchedItem entity: fields, schedule resolution, registry reconciliation, notifications
- [docs/WATCHED-ITEMS-DASHBOARD.md](docs/WATCHED-ITEMS-DASHBOARD.md) — the operator surface: API/dashboard routes, lifecycle guards, list and detail views, audit parity
