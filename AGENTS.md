# watcher — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Sacrifice grammar for density. Lead with the answer or action.

## Project Overview

Web service for monitoring cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff; Node.js + npm (Tailwind CLI — `sudo npm install -g @tailwindcss/cli@4.2.4`; the pin is load-bearing, a newer CLI rebuilds `output.css` and `check-css.sh` calls it stale).

**Cannobserv wheelhouse.** Populate it before any `uv` command — `[tool.uv]
find-links` makes every invocation require the directory:

```bash
uv run --no-project --with 'google-cloud-storage>=2,<4' python scripts/sync_wheelhouse.py
uv sync
```

Auth, upgrade procedure and the pinned version: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) → *Cannobserv wheelhouse*.
`co-core` owns fetch → extract → fingerprint; watcher no longer fetches at all —
[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md).

<!-- BEGIN socraticode-policy -->
## Code Exploration Policy

SocratiCode indexes into the cohort's **shared store on `co-index`, never locally** (#300); `includeLinked: true` also answers from sibling repos, none on disk. Its MCP tools are **deferred**: run the SessionStart hook's `ToolSearch` prefetch first. The daily health hook **reports only** — confirm with `codebase_status` before acting on it.

**Negative rule.** Semantic questions go to SocratiCode first; `grep`/`rg` only for exact strings; the Explore subagent only for path-pattern walks (`*.py` under `src/api/routes/`). **Empty is not absent:** an unreachable collection is skipped silently, so verify the store before trusting a miss, then `grep` for that session.

| Goal | Tool |
|---|---|
| Where is X / how does Y work / what touches Z | `codebase_search` |
| Exact string (errors, log lines, known symbols) | `grep` / `rg` |
| Imports/dependents of a file · blast radius | `codebase_graph_query` / `codebase_impact` |

Full tool table, index scope, client contract and traps: [docs/SOCRATICODE.md](docs/SOCRATICODE.md).
<!-- END socraticode-policy -->

## Infrastructure

**Single-VM setup.** Dev and prod on the same VM. Code committed to `main` is the deployed code.

| Service | Port | Managed by |
|---|---|---|
| API (live) | 8000 | `systemctl` (`watcher.service`) |
| API (dev) | 8001 | manual uvicorn |

The exe.dev proxy forwards 3000–9999; dev server at `https://co-watcher.exe.xyz:8001/`.

**Single process is load-bearing.** One uvicorn process runs everything — API, embedded Procrastinate worker, `content.blobs` fact consumer, cache sweeper. **Never `uvicorn --workers N`, never a second worker unit against prod.** Why, and the escalation path that is *not built*: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Single process*.

**Host memory is the shared resource (#307).** No swap, and agent sessions are *unkillable* (`oom_score_adj` -1000), so the kernel's killer takes the service. SocratiCode is **pinned pre-installed** (`~/.socraticode/pin`): never let a launch install a server. The unit takes a **reservation, never a cap** (`MemoryHigh=` stalls it while it still reports `active`), holding only while **every slice above grants as much** (#309). Verify, re-pin, the drop-ins: [docs/HOST-MEMORY.md](docs/HOST-MEMORY.md).

**The bus.** The broker is its own VM (`broker`, CannObserv/broker); watcher publishes four streams and consumes two — `content.blobs` (group `watcher.blobs`) and `info.registry` (**groupless**, replayed from `0-0` every boot). `WATCHER_BUS_REDIS_URL` unset → publish tasks skip loudly. Inventory, ownership, fetch contracts, `info_source_id` on the wire: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Redis and the bus*.

**Retention is sized against the set (#292), and mirrored on the broker (#319).** `tests/test_bus_stream_kinds.py` fails a config/state publish missing `maxlen` *or* `floor=`, and fails a retune of any mirrored number — the broker's probe thresholds are computed from copies, so the fix is cross-repo. The env overrides move the same numbers unseen (same section).

**Connection policy (#287, #288, #290).** `socket_timeout` is a **floor** derived from `src/core/read_windows.py`, never transcribed; retries are an explicit **zero** (a retry re-sends the command). `OutOfMemoryError` (full broker) and `NoPermissionError` (ACL) are `ResponseError`s, **not** connection errors — keep both transient in every producer: [docs/BUS-CONNECTION-POLICY.md](docs/BUS-CONNECTION-POLICY.md).

## Server Lifecycle

**Port 8000 belongs to systemd. Never start uvicorn manually on port 8000.**

After committing to `main`: `sudo systemctl restart watcher`. After DB model changes: `uv run alembic upgrade head` then restart. After Tailwind/vendor CSS changes: `bash scripts/build-css.sh` then restart. Logs: `sudo journalctl -u watcher -f`.

Dev server (port 8001, leaves prod alone):

```bash
bash scripts/dev_server.sh
```

**Never launch uvicorn by hand with the prod env loaded** — it shares the prod DB and runs a second worker on the prod queue (#233). `scripts/dev_server.sh` and `src/core/db_safety.py` both refuse any DB whose name lacks a `_test`/`_dev` suffix. Full rationale: [docs/COMMANDS.md](docs/COMMANDS.md) → *Development*.

**Archiver owns the canonical registry**; watcher consumes it over the bus, makes **no HTTP calls to Archiver at all** and reads no Archiver checkout (#311) — re-adding an SDK is a design regression, and Archiver code does not belong in this repo: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Sibling services*.

**Cross-repo policy.** Never edit sibling repos (`archiver`, `notifier`) from a watcher conversation. Identify the gap, recommend it, get approval, then file a GH issue in that repo; implementation is a separate session scoped to it.

**Nothing in `src/` mirrors to Archiver** (#159, #236) — don't reintroduce a sync obligation: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *No cross-repo mirror discipline*.

## Environment Files

Shells load two env files in order (later overrides earlier); the service loads only the first (#296 D5):

1. `/etc/watcher/.env` — production secrets (`DATABASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`). Persistent, managed manually on the VM.
2. `.env` (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`). Never commit.

Plus `/etc/watcher/notifier.env` (600 root:root, `WATCHER_NOTIFIER_BASE_URL` + `WATCHER_NOTIFIER_API_KEY`): `deploy/watcher.service` loads it, nothing else may read it. **Never source, copy, or re-add those names to a shared env file** (#278) — a backup beside the original counts. Non-production runs use notifier's dev tenant via `WATCHER_DEV_NOTIFIER_*`: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

Load both into a shell (pytest, psql, gh):

```bash
source scripts/load-env.sh
```

**Naming rule.** Anything naming a shared external resource takes a **service-prefixed** name plus a dev key (`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL`): a bare `REDIS_URL` is silently inherited from `/etc/watcher/.env`, the #233 hazard in env-var form. [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) → *Environment Variables*.

**A URL is configuration, not permission.** Three unit-only opt-ins gate production resources — `WATCHER_ALLOW_PRODUCTION_DB` (#233), `WATCHER_BUS_ENABLED` (#262), `WATCHER_NOTIFIER_ENABLED` (#277) — and never belong in an env file. A URL without its flag aborts startup — and for the notifier, so does the **flag held without a URL**, which means the unit lost `/etc/watcher/notifier.env` (#278). `scripts/dev_server.sh` and `tests/conftest.py` clear what they did not set.

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
`alembic.ini` carries no URL, so an unloaded shell fails rather than reaching
production.

## Watched Items

**The `WatchedItem` is the single monitored entity (#191).** One `WatchedItem` =
one URL = one fingerprint = one change signal. The user-facing noun is "Watched
Item".

**The `info.registry` reconcile is the creation path**; the registry owns
cadence and active state, Watcher owns mechanism (#254). An announcement is
authoritative for a named set of columns, everything else survives
reconciliation, and **a local pause is not sticky** — every announcement-owned
field 409s locally on a reconciled item.

**Empty extraction is a failure, not a change (#258)** — a `source_spec`
yielding empty chunks raises `ExtractionError` and writes nothing, either side
of a baseline. **An unchanged fingerprint still announces (#293)** after a full
fetch — never a 304, never the baseline — and a renewal may only improve a
queued row:
[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md).

What each 409 is, where pause does live, the authoritative column list: [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md).

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

JSON records with a four-key floor pinned by `tests/core/test_logging.py`; why uvicorn's loggers need `--log-config` plus a filter: [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

**Date & Time:** All UTC. ISO 8601: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (timestamps), `YYYY-MM-DD` (dates).

**General:**
- No inline module imports; all at file top
- Docstrings for public modules, classes, functions
- Test structure mirrors source (`src/foo.py` → `tests/test_foo.py`)
- Optional JSONB columns: declare as `JSONB(none_as_null=True)` so Python `None` persists as SQL `NULL`, not a JSONB `'null'` literal (otherwise `WHERE col IS NULL` silently misses those rows — #198)

**ULID format errors:** path parameter → 404 (`parse_ulid`), filter query parameter → 400 (`parse_filter_ulid`). **DB triggers:** currently none; one added in a migration must also be recreated in `tests/conftest.py`'s `test_engine` fixture (integration tests build the schema with `create_all`). Both: [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

## Style & UI

Design system: [docs/STYLE.md](docs/STYLE.md); component library and HTMX/flash
patterns: [docs/UI.md](docs/UI.md). **Read one of them before writing a
template**; their rules, in brief: brand color is never a status color (STYLE
§2), every color utility takes its `dark:` variant (§3), WCAG 2.1 AA and no
`title` attributes (§7–8), never a CDN build (§10), component classes over raw
utilities (UI §4), and `is_htmx(request)` rather than a bare `HX-Request` read
(UI §2, #211).

## Agent Skills

A skill is symlinked into both `skills/` and `.claude/skills/`; overrides in `skills/` shadow `skills-vendor/`: [docs/SKILLS.md](docs/SKILLS.md).

## Detail Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module layout, sibling services, bus topology, fetch contracts, the probe destination guard
- [docs/BUS-CONNECTION-POLICY.md](docs/BUS-CONNECTION-POLICY.md) — #287 timeouts, retries, redaction, startup PING; #288 the `noeviction` cap
- [docs/COMMANDS.md](docs/COMMANDS.md) — every runnable command, the test database, CI
- [docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md) — fetch → extract → fingerprint, the outbox, the revisions producer
- [docs/CONDITIONAL-GET.md](docs/CONDITIONAL-GET.md) — #269 validators: gate, snapshot, invalidation
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — logging configuration, ULID errors, DB triggers
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — systemd units, the install runbook, timers, wheelhouse auth
- [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) — every env file and variable, load order, the unit-only credentials
- [docs/HOST-MEMORY.md](docs/HOST-MEMORY.md) — #307/#309: reservations, slices, earlyoom, the SocratiCode pin
- [docs/RECOVERY.md](docs/RECOVERY.md) — nightly DB backup to GCS, restore, go/no-go gates; dated [rehearsals](docs/RECOVERY-REHEARSALS.md)
- [docs/MIGRATIONS.md](docs/MIGRATIONS.md) — the manual upgrade step, the two-role grants, one-time orderings
- [docs/reference/tailscale.md](docs/reference/tailscale.md) — this node: identity, peers, the cold-boot race, ACL rules
- [docs/SKILLS.md](docs/SKILLS.md) — skill triggers, vendored skill repos, the SessionStart hooks
- [docs/SOCRATICODE.md](docs/SOCRATICODE.md) — tool table, index scope, the co-index client contract, green-failing traps, link stubs
- [docs/STYLE.md](docs/STYLE.md) — the design system: brand, color, dark mode, tokens, layout, touch targets, accessibility
- [docs/UI.md](docs/UI.md) — the component library, the HTMX/flash patterns
- [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md) — the entity: fields, schedule resolution, reconciliation, domain keying, media-type dispatch, template CRUD, notifications
- [docs/WATCHED-ITEMS-DASHBOARD.md](docs/WATCHED-ITEMS-DASHBOARD.md) — the operator surface: routes, lifecycle guards, views, audit parity
