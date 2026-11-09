# watcher — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Sacrifice grammar for density. Lead with the answer or action.

## Project Overview

General-purpose web monitoring service: fingerprints the content at URLs Archiver registers on a schedule and reports each change.

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff.

**co-pypi index (#358).** co-core is private: a `uv lock`, or a `uv sync` that
downloads, needs a per-command token — never in an env file:

```bash
source scripts/load-env.sh && bash scripts/co-pypi.sh uv sync
```

Auth, upgrade procedure and the pinned version: [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md).
`co-core` owns fetch → extract → fingerprint; watcher does none of it —
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

**Single process is load-bearing.** One uvicorn process runs everything — API, embedded Procrastinate worker, fact consumers, cache sweeper. **Never `uvicorn --workers N`, never a second worker unit against prod.** Why, and the escalation path that is *not built*: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Single process*.

**Host memory is the shared resource (#307).** SocratiCode is **pinned pre-installed** (`~/.socraticode/pin`): never let a launch install a server. The unit takes a **reservation, never a cap** (#309). Session `oom_score_adj` (#337), slices, verify, re-pin: [docs/HOST-MEMORY.md](docs/HOST-MEMORY.md).

**The bus.** The broker is its own VM (`broker`, CannObserv/broker); watcher publishes five streams and consumes four — `info.registry` is **groupless**, replayed from `0-0` every boot. Inventory, groups, ownership: [docs/BUS.md](docs/BUS.md).

**Connection policy (#287, #288, #290).** `socket_timeout` is derived, never transcribed; retries are **zero**; a full broker and an ACL denial are `ResponseError`s that every producer keeps transient: [docs/BUS-CONNECTION-POLICY.md](docs/BUS-CONNECTION-POLICY.md).

## Server Lifecycle

**Port 8000 belongs to systemd. Never start uvicorn manually on port 8000.**

After committing to `main`: `sudo systemctl restart watcher`. After DB model changes: `uv run alembic upgrade head` then restart. Logs: `sudo journalctl -u watcher -f`.

Dev server (port 8001, leaves prod alone):

```bash
bash scripts/dev_server.sh
```

**Never launch uvicorn by hand with the prod env loaded** — it shares the prod DB and runs a second worker on the prod queue (#233). `scripts/dev_server.sh` and `src/core/db_safety.py` both refuse any DB whose name lacks a `_test`/`_dev` suffix. Full rationale: [docs/COMMANDS.md](docs/COMMANDS.md) → *Development*.

**Archiver owns the canonical registry**; watcher consumes it over the bus, makes **no HTTP calls to Archiver at all** and reads no Archiver checkout (#311) — re-adding an SDK is a design regression, and neither Archiver code nor a sync obligation to it belongs in `src/` (#159, #236): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) → *Sibling services*.

**Cross-repo policy.** Never edit sibling repos (`archiver`, `notifier`) from a watcher conversation. Identify the gap, recommend it, get approval, then file a GH issue in that repo; implementation is a separate session scoped to it.

## Environment Files

Shells load two env files in order (later overrides earlier); the service loads only the first (#296 D5):

1. `/etc/watcher/.env` — production secrets (`DATABASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`). Persistent, managed manually on the VM.
2. `.env` (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`). Never commit.

The notifier credentials and their unit-only file are retired (#379); `tests/test_notification_subsystem_retired.py` forbids their names. History: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

Load both into a shell (pytest, psql, gh):

```bash
source scripts/load-env.sh
```

**Naming rule.** Anything naming a shared external resource takes a **service-prefixed** name plus a dev key (`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL`): a bare `REDIS_URL` is silently inherited from `/etc/watcher/.env`, the #233 hazard in env-var form. [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) → *Environment Variables*.

**A URL is configuration, not permission.** Two unit-only opt-ins gate production resources — `WATCHER_ALLOW_PRODUCTION_DB` (#233), `WATCHER_BUS_ENABLED` (#262) — and never belong in an env file. A URL without its flag aborts startup. `scripts/dev_server.sh` and `tests/conftest.py` clear what they did not set.

## Common Commands

```bash
bash scripts/co-pypi.sh uv sync              # install deps (token: above)
uv run pre-commit install                    # once per clone: ruff hooks
uv run pytest                                # tests
uv run pytest -m integration                 # integration tests (test DB only; CI + pre-ship run them)
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

**The `info.registry` reconcile is the only path that creates or edits an
item** (#378); the registry owns cadence and active state, Watcher owns
mechanism (#254). An announcement is authoritative for a named set of columns,
and everything else survives reconciliation.

**Empty extraction is a failure, not a change (#258)**, and **an unchanged
fingerprint still announces (#293)**:
[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md),
[docs/CONTENT-REVISIONS.md](docs/CONTENT-REVISIONS.md).

Where pause does live, the authoritative column list: [docs/REGISTRY.md](docs/REGISTRY.md).

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

**ULIDs:** canonical uppercase only (`parse_canonical_ulid`). **DB triggers:** none; one added in a migration is also recreated in `tests/conftest.py`. Both: [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

## Agent Skills

A skill is symlinked, unforked, into both `skills/` and `.claude/skills/` (#413): [docs/SKILLS.md](docs/SKILLS.md).

**`brainstorming`, overriding its text (#396):** explicit ask or undesigned feature only; doc → `docs/plans/` → issue → `using-git-worktrees`; `writing-plans` optional. Companion: SKILLS.md *Design work*.

## Detail Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module layout, sibling services, fetch contracts
- [docs/BUS.md](docs/BUS.md) — the broker, stream inventory and retention, consumer groups
- [docs/BUS-CONNECTION-POLICY.md](docs/BUS-CONNECTION-POLICY.md) — #287 timeouts, retries, redaction, startup PING; #288 the `noeviction` cap
- [docs/COMMANDS.md](docs/COMMANDS.md) — every runnable command, the test database, CI
- [docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md) — fetch → extract → fingerprint, the `fetch_commands` outbox
- [docs/CONTENT-REVISIONS.md](docs/CONTENT-REVISIONS.md) — the revisions producer: outbox, drain, #293 renewal
- [docs/WATCH-EVENTS.md](docs/WATCH-EVENTS.md) — #369 edge facts: outbox, ordered drain, broker#93 gate
- [docs/WATCH-CHECK.md](docs/WATCH-CHECK.md) — #371 `info.watch-check`: shared pre-flight, rejections, DLQ, the completion republish
- [docs/CONDITIONAL-GET.md](docs/CONDITIONAL-GET.md) — #269 validators: gate, snapshot, invalidation
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — logging configuration, ULID parsing, DB triggers
- [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) — the co-pypi index, the certifi and advisory floors, Dependabot PRs: auth, pins, upgrades
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — systemd units, the install runbook, timers, OS patching (#382)
- [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) — every env file and variable, load order, the unit-only opt-ins
- [docs/HOST-MEMORY.md](docs/HOST-MEMORY.md) — #307/#309: reservations, slices, earlyoom, the SocratiCode pin
- [docs/RECOVERY.md](docs/RECOVERY.md) — nightly DB backup to GCS, restore, go/no-go gates; dated [rehearsals](docs/RECOVERY-REHEARSALS.md), one-time [provisioning](docs/RECOVERY-PROVISIONING.md)
- [docs/MIGRATIONS.md](docs/MIGRATIONS.md) — the manual upgrade step, the two-role grants, one-time orderings
- [docs/reference/tailscale.md](docs/reference/tailscale.md) — this node: identity, peers, the cold-boot race, ACL rules
- [docs/SKILLS.md](docs/SKILLS.md) — skill triggers, vendored skill repos, the SessionStart hooks
- [docs/SOCRATICODE.md](docs/SOCRATICODE.md) — tool table, index scope, green-failing traps, link stubs
- [docs/CO-INDEX.md](docs/CO-INDEX.md) — the shared store on `co-index`: the client contract, verifying it before trusting a miss
- [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md) — the entity: fields, schedule resolution, domain keying, media-type dispatch
- [docs/REGISTRY.md](docs/REGISTRY.md) — `info.registry` reconciliation: what an announcement owns, linkage
