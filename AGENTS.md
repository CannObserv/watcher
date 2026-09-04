# watcher — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Sacrifice grammar for density. Lead with the answer or action.

## Project Overview

Web service for monitoring cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff; Node.js + npm (Tailwind CLI — `sudo npm install -g @tailwindcss/cli`, one-time VM setup).

**Cannobserv wheelhouse.** Populate it before any `uv` command — `[tool.uv]
find-links` makes every invocation require the directory:

```bash
uv run --no-project --with 'google-cloud-storage>=2,<4' python scripts/sync_wheelhouse.py
uv sync
```

Auth, upgrade procedure and the pinned version: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) → *Cannobserv wheelhouse*.
`co-core` owns fetch → extract → fingerprint; watcher no longer fetches at all —
[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md).

## Code Exploration Policy

SocratiCode is indexed on this repo (`.socraticodecontextartifacts.json` present). Its MCP tools are **deferred** — schemas load only after a `ToolSearch` prefetch. The SessionStart hook prints the prefetch query; run it before exploring.
A second, daily health hook **reports only** — confirm with `codebase_status` before acting on it.

**Negative rule.** For broad semantic questions ("where is X", "how does Y work", "what depends on Z"), use SocratiCode MCP tools first. Reach for `grep`/`ripgrep` only on exact strings (error messages, log lines, known symbols). Reserve the Explore subagent for path-pattern walks (e.g. "all `*.py` under `src/api/routes/`"), not semantic search.

The goal→tool table, index scope and rebuild, and the literal prefetch query: [docs/SKILLS.md](docs/SKILLS.md).

## Infrastructure

**Single-VM setup.** Dev and prod on the same VM. Code committed to `main` is the deployed code. Systemd service `watcher` runs the live site on port 8000.

| Service | Port | Managed by |
|---|---|---|
| API (live) | 8000 | `systemctl` (`watcher.service`) |
| API (dev) | 8001 | manual uvicorn |
| Archiver | 8020 | `systemctl` (`archiver.service`) |

`ARCHIVER_REPO_PATH` redirects everything needing the sibling repo (#254): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

The exe.dev proxy forwards 3000–9999; dev server at `https://watcher.exe.xyz:8001/`.

**Single process is load-bearing.** One uvicorn process runs everything — API, embedded Procrastinate worker, `content.blobs` fact consumer, cache sweeper. **Never run `uvicorn --workers N` or a second worker unit against prod.** Why the fact consumer makes this load-bearing, and the escalation path that is *not built*: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Single process*.

**The bus.** Archiver operates the broker; watcher publishes four streams and consumes two — `content.blobs` (single-member group `watcher.blobs`, derived by co-core's `group_name` — #285) and `info.registry` (**groupless**, replayed from `0-0` every boot). `WATCHER_BUS_REDIS_URL` unset → publish tasks skip loudly. Stream inventory and ownership, the fetch contracts, `info_source_id` on the wire: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Redis and the bus*.

**Connection policy (#287).** `socket_timeout` is a **floor**, not a ceiling — derive it from `src/core/read_windows.py`, never transcribe a window; retries are an explicit **zero** (a redis-py retry re-sends the command). [docs/BUS-CONNECTION-POLICY.md](docs/BUS-CONNECTION-POLICY.md).

## Server Lifecycle

**Port 8000 belongs to systemd. Never start uvicorn manually on port 8000.**

After committing to `main`: `sudo systemctl restart watcher`. After DB model changes: `uv run alembic upgrade head` then restart. After Tailwind/vendor CSS changes: `bash scripts/build-css.sh` then restart. Logs: `sudo journalctl -u watcher -f`.

Dev server (port 8001, leaves prod alone):

```bash
bash scripts/dev_server.sh
```

**Never launch uvicorn by hand with the prod env loaded** — it shares the prod DB and runs a second worker on the prod queue (#233). `scripts/dev_server.sh` and `src/core/db_safety.py` both refuse any DB whose name lacks a `_test`/`_dev` suffix. Full rationale: [docs/COMMANDS.md](docs/COMMANDS.md) → *Development*.

**Archiver owns the canonical registry**; watcher consumes it over the bus and makes **no HTTP calls to Archiver at all** — re-adding an SDK is a design regression. Don't add Archiver code to this repo: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Sibling services*.

**Cross-repo policy.** Do not directly edit sibling repos (`archiver`, `notifier`) within a watcher conversation. If a change to a sibling is needed: identify the gap, recommend it, get approval, then file a GH issue in that repo. Implementation happens in a separate session scoped to the sibling.

**Nothing in `src/` mirrors to Archiver** (#159, #236) — don't reintroduce a sync obligation: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *No cross-repo mirror discipline*.

## Environment Files

Two env files load in order (later overrides earlier):

1. `/etc/watcher/.env` — production secrets (`DATABASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`). Persistent, managed manually on the VM.
2. `.env` (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`). Never commit.

Plus `/etc/watcher/notifier.env` (600 root:root, `WATCHER_NOTIFIER_BASE_URL` + `WATCHER_NOTIFIER_API_KEY`): `deploy/watcher.service` loads it, nothing else may read it. **Never source, copy, or re-add those names to a shared env file** (#278) — a backup beside the original counts. Non-production runs use notifier's dev tenant via `WATCHER_DEV_NOTIFIER_*`: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

Load both for shell commands (pytest, psql, gh):

```bash
source scripts/load-env.sh
```

**Naming rule for new variables.** Anything naming a shared external resource takes a **service-prefixed** name with a separate dev key (`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL`). A bare `REDIS_URL` is silently inherited from `/etc/watcher/.env` — the #233 hazard in env-var form: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) → *Environment Variables*.

**A URL is configuration, not permission.** Three unit-only opt-ins gate the production resources — `WATCHER_ALLOW_PRODUCTION_DB` (#233), `WATCHER_BUS_ENABLED` (#262), `WATCHER_NOTIFIER_ENABLED` (#277). Never put one in an env file; a URL held without its flag aborts startup — and for the notifier, so does the **flag held without a URL**, which means the unit lost `/etc/watcher/notifier.env` (#278). `scripts/dev_server.sh` and `tests/conftest.py` clear what they did not set.

## Common Commands

```bash
uv sync                                      # install deps
uv run pytest                                # tests
uv run pytest -m integration                 # integration tests (needs PostgreSQL)
uv run ruff check .                          # lint
uv run alembic upgrade head                  # apply migrations
```

**Never run `alembic revision --autogenerate` against `DATABASE_URL`** — it diffs
the models against production. Build a scratch database first (#259):
[docs/COMMANDS.md](docs/COMMANDS.md) → *Autogenerate wants a scratch database*.
Alembic connects with `WATCHER_MIGRATION_DATABASE_URL`, else `DATABASE_URL`;
`alembic.ini` carries no URL, so an unloaded shell fails rather than defaulting
to production.

## Watched Items

**The `WatchedItem` is the single monitored entity (#191).** One `WatchedItem` =
one URL = one fingerprint = one change signal; the earlier `Watch` model is
gone. The user-facing noun is "Watched
Item".

**The `info.registry` reconcile is the creation path**, and the registry owns
cadence and active state while Watcher owns mechanism (#254): an announcement is
authoritative for a named set of columns, everything else survives reconciliation,
and **a local pause is not sticky** — item-level pause lives in Archiver's
dashboard alone, and every announcement-owned field 409s locally on a reconciled
item. `POST /api/v1/watched-items` still works but has had no caller since
archiver#158. What each 409 is: [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md).

**Empty extraction is a failure, not a change (#258).** Every `source_spec`
yielding empty chunks raises `ExtractionError` and writes nothing —
unconditionally, on both sides of a baseline:
**[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md)**.

**Conditional GET is gated and item-scoped (#269)** — off unless the item is
named in `WATCHER_CONDITIONAL_GET_ENABLED`, and a 304 inherits the last
fingerprint, so a spec/URL/extractor change must invalidate the stored pair:
[docs/CONDITIONAL-GET.md](docs/CONDITIONAL-GET.md).

**Notifications.** One `notification_templates` table; a row's `visibility` —
`global` / `domain` / `watched_item` — decides where it fires. **Bodies are
source Markdown and must be block-structured**, never `\n`-joined — guarded by
`tests/core/notifications/test_content.py::TestMarkdownListContract`.

Fields, what each 409 is, the authoritative column list, schedule resolution, domain keying, media-type dispatch, template CRUD: [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md). Lifecycle, delete guards, every dashboard surface: [docs/WATCHED-ITEMS-DASHBOARD.md](docs/WATCHED-ITEMS-DASHBOARD.md).

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

Records are JSON with a four-key floor — `timestamp`/`level`/`logger`/`message` — pinned by `tests/core/test_logging.py`. The floor, and why uvicorn's own loggers need `--log-config` plus a filter: [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

**Date & Time:** All UTC. ISO 8601: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (timestamps), `YYYY-MM-DD` (dates).

**General:**
- No inline module imports; all at file top
- Docstrings for public modules, classes, functions
- Test structure mirrors source (`src/foo.py` → `tests/test_foo.py`)
- Explicit imports only
- Small, focused functions
- Optional JSONB columns: declare as `JSONB(none_as_null=True)` so Python `None` persists as SQL `NULL`, not a JSONB `'null'` literal (otherwise `WHERE col IS NULL` silently misses those rows — #198)

**ULID format errors:** path parameter → 404 (`parse_ulid`), filter query parameter → 400 (`parse_filter_ulid`). **DB triggers:** currently none; one added in a migration must also be recreated in `tests/conftest.py`'s `test_engine` fixture (integration tests build the schema with `create_all`). Both: [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

## Style & UI

Design system: [docs/STYLE.md](docs/STYLE.md). Component library and the
HTMX/flash patterns: [docs/UI.md](docs/UI.md). Both authoritative; what follows
is only what is easy to get wrong.

**Brand:** Cannabis Observer — `co-purple-600` (#6d4488) primary accent. Never use brand colors for semantic status (green/yellow/red/blue).

**Dark Mode:** Tailwind `dark:` variants on every color utility. Class-based toggle (`<html class="dark">`), localStorage key `watcher-color-scheme`.

**Accessibility:** WCAG 2.1 AA, and no `title` attributes. **Touch-target idiom (#203):** component classes own the 44px guarantee — never restate `min-h-[44px]` on a `.btn`, never `min-h-0`. [docs/STYLE.md](docs/STYLE.md) §7–8 (guards: `tests/dashboard/test_touch_targets.py`, `scripts/check-touch-targets.sh`).

**CSS:** Tailwind v4 with `@theme` in `input.css`; use the component classes rather than raw utilities, and never a CDN build.

**HTMX:** **Detect HTMX with `is_htmx(request)`** ([src/dashboard/deps.py](src/dashboard/deps.py)), never a bare `HX-Request` read — guarded by `tests/dashboard/test_htmx_detection.py` (#211). OOB flash via `partials/flash_oob.html`.

## Agent Skills

Skills live in `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Local overrides in `skills/` shadow vendor submodules in `skills-vendor/`.

Cross-project search to the sister `notifier` index requires a per-instance `.claude/settings.local.json` (gitignored) — see "Linked Projects" in `docs/SKILLS.md`.

## Detail Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module layout, sibling services, the Archiver checkout constraint, bus topology and fetch contracts
- [docs/COMMANDS.md](docs/COMMANDS.md) — every runnable command, the Archiver-sibling test setup, CI
- [docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md) — fetch → extract → fingerprint, the fetch-command outbox, the revisions producer
- [docs/BUS-CONNECTION-POLICY.md](docs/BUS-CONNECTION-POLICY.md) — #287 bus client timeouts, retries, redaction, startup PING
- [docs/CONDITIONAL-GET.md](docs/CONDITIONAL-GET.md) — #269 validators: gate, snapshot, invalidation
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — logging configuration, ULID error handling, DB-trigger rules
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — systemd units, the install runbook, timers, wheelhouse auth
- [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) — every env file and variable, load order, the unit-only credentials
- [docs/MIGRATIONS.md](docs/MIGRATIONS.md) — the manual upgrade step, the two-role grant model, one-time orderings
- [docs/SKILLS.md](docs/SKILLS.md) — skill triggers, vendored skill repos, SocratiCode workflow
- [docs/STYLE.md](docs/STYLE.md) — the design system: brand, color, dark mode, tokens, layout, touch targets, accessibility
- [docs/UI.md](docs/UI.md) — the component library and the HTMX/flash interaction patterns
- [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md) — the entity: fields, schedule resolution, registry reconciliation, notifications
- [docs/WATCHED-ITEMS-DASHBOARD.md](docs/WATCHED-ITEMS-DASHBOARD.md) — the operator surface: routes, lifecycle guards, list and detail views, audit parity
