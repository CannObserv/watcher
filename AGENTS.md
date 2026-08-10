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

Wheelhouse auth (ADC on the VM, keyless WIF in CI), the upgrade procedure, and the
currently pinned `co-core` version: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) → *Cannobserv wheelhouse*.
`co-core` owns fetch → extract → fingerprint (the `extract` extra); watcher no longer
fetches at all — [docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md).

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

## Infrastructure

**Single-VM setup.** Dev and prod on the same VM. Code committed to `main` is the deployed code. Systemd service `watcher` runs the live site on port 8000.

| Service | Port | Managed by |
|---|---|---|
| API (live) | 8000 | `systemctl` (`watcher.service`) |
| API (dev) | 8001 | manual uvicorn |

Sibling services on the same VM, separately managed: **Archiver** (port 8020, `archiver.service`) and **Notifier**. Both are separate repos checked out alongside this one (`/home/exedev/archiver`, `/home/exedev/notifier` on this VM). Elsewhere in these docs they're named as "the Archiver repo" / "the Notifier repo" — resolve those against your own checkout.

**The Archiver checkout is not freely relocatable.** `ARCHIVER_REPO_PATH` redirects the
test harness alone; the `archiver-client` path dependency in `pyproject.toml` is pinned
to `../archiver/clients/python` separately and honors no env var. Setting one without
the other yields passing tests over a broken `uv sync`.

The exe.dev proxy forwards 3000–9999. Dev server reachable at `https://watcher.exe.xyz:8001/`.

**Single process is load-bearing.** One uvicorn process runs everything: the API, the embedded Procrastinate worker, the `content.blobs` fact consumer, and the cache sweeper (started in the `src/api/main.py` lifespan — there is no separate worker unit). The reason is now the **fact consumer**, not politeness: `src/workers/fetch_facts.py` joins consumer group `watcher` as a single member (`watcher-1`), and a second process would need its own consumer name *and* an apply-ordering story across members — the supersession guard is per-row, not a cross-process lock. (Until #241 step 5 the reason was the in-process `DomainRateLimiter`; that retired with the local fetch path, so per-host pacing no longer constrains the topology at all — it is Replicator's, fed over `content.fetch-policy`.) Never run `uvicorn --workers N` or a second worker unit against prod. Escalation path when one process stops being enough (not before): a separate `watcher-worker.service` plus a multi-member consumer-group design — **not built**.

**The bus.** Watcher publishes `content.fetch` (commands), `content.fetch-policy`
(per-host politeness), and `content.revisions` (`source_revision_observed`), and consumes
`content.blobs` as the single member of consumer group `watcher`. Archiver operates the
broker. `WATCHER_BUS_REDIS_URL` unset → the publish tasks skip loudly rather than
silently. Stream ownership, the fetch contracts, and `info_source_id` on the wire:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
- `WATCHER_BUS_REDIS_URL` — Broker URL for the `content.fetch-policy` producer (#245; prod: `redis://localhost:6379/0`). Unset → publish task skips loudly. Dev opts in via `WATCHER_DEV_BUS_REDIS_URL` (see **Redis and the bus**).

Full variable reference: `docs/DEPLOYMENT.md`.

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

## Watched Items

**The `WatchedItem` is the single monitored entity (#191).** The earlier
`WatchedItem → Watch` one-to-many was collapsed: `Watch` (the model, table,
`/watches*` routes, the override resolution chain, and the per-Watch
notification tier) is gone. One `WatchedItem` = one URL = one fingerprint = one
change signal. The user-facing noun is "Watched Item".

Created **only** by `POST /api/v1/watched-items`, which requires all three of
`archiver_info_item_id` + `url` + `archiver_info_source_id` — every WatchedItem is an
Archiver InfoItem being watched, and there is no dashboard create form.
`WatchedItem.domain_name` == `Domain.name` == `hostname(effective_url)` by construction;
one entry per hostname, so host variants (`lcb.wa.gov` vs `www.lcb.wa.gov`) are
independent by design.

**Empty extraction is a failure, not a change (#258).** When every `source_spec`
yields empty chunks, `process_watched_item` raises `ExtractionError` and writes
nothing — unconditionally, on both sides of a baseline. Before the guard,
selector rot presented as a *content change* with health still OK. Full
rationale, and the six provenance columns the outbox gained for
`source_revision_observed` (#253): **[docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md)**.

**Notifications.** One table — `notification_templates` — holds every notification
target, and each row's intrinsic `visibility` decides where it fires: `global` (every
item), `domain` (every item on a matching `domain_name`), or `watched_item` (one item).
A CHECK constraint enforces that exactly the implied ref column is set.

**Notification bodies are source Markdown, and must be block-structured** — the
`change_detected` body is a bullet list, because CommonMark turns a lone `\n` into a
soft break and a `\n`-joined paragraph collapses to one run-on line on HTML clients.
Guarded by `tests/core/notifications/test_content.py::TestMarkdownListContract`; keep it
that way when editing the composer.

Fields, 3-tier schedule resolution, media-type dispatch, lifecycle and delete guards,
template CRUD, and every dashboard surface: [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md).

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

Every record serializes as JSON with **at least** `timestamp` / `level` / `logger` /
`message`, plus `exc_info` and whatever extras the emitting library attaches. Those four
are a floor, not an exhaustive list, and are pinned by `tests/core/test_logging.py` —
don't rename or drop a key without updating both. Why that set, and the rest of the
logging configuration: [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

uvicorn's own loggers need `--log-config src/core/log_config.json` (both sanctioned launch
paths already pass it) plus the `strip_color_message` filter; `ExecStartPre` output is
plain text by design, so a log pipeline must tolerate non-JSON journald lines.
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

**Accessibility:** WCAG 2.1 AA. Skip link, ARIA landmarks, `focus-visible` rings, 44px touch targets, `aria-live` on HTMX swap targets, reduced motion. Wrap decorative emoji in `<span aria-hidden="true">`. No `title` attributes. **Touch-target idiom (#203):** component classes (`.btn*`, `.segment`, `.chip`, `.form-input`, `.toggle`, nav-link) own the 44px guarantee — never restate `min-h-[44px]` on a `.btn`; use it only on bare interactive elements (`<a>`, `<label>`, component-less `<button>`); never `min-h-0`. Guard: `tests/dashboard/test_touch_targets.py` + `scripts/check-touch-targets.sh`. See `docs/STYLE.md` §7.

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.segment-group`, `.segment`, `.chip-group`, `.chip`, `.detail-grid`, `.toggle`, `.danger-zone`). Badge variants: `.badge-active` (green), `.badge-inactive` (gray), `.badge-archived` (amber), `.badge-error` (red), `.badge-warning` (orange), `.badge-info` (blue). Use CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. Detect HTMX via the canonical `is_htmx(request)` helper ([src/dashboard/deps.py](src/dashboard/deps.py)) — `HX-Request` header with `HX-Boosted` guard, so a boosted full-page nav stays on the non-HTMX path — never a bare `request.headers.get("HX-Request")` read (guarded by `tests/dashboard/test_htmx_detection.py`; #211). All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.

## Agent Skills

Skills live in `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Local overrides in `skills/` shadow vendor submodules in `skills-vendor/`.

Full skill reference: `docs/SKILLS.md`. Cross-project search to the sister `notifier` index requires a per-instance `.claude/settings.local.json` (gitignored) — see "Linked Projects" in `docs/SKILLS.md`.

## Detail Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module layout, the Archiver checkout constraint, and the Redis bus topology, streams, and fetch contracts
- [docs/COMMANDS.md](docs/COMMANDS.md) — every runnable command, the Archiver-sibling test setup, and CI
- [docs/CONTENT-PIPELINE.md](docs/CONTENT-PIPELINE.md) — fetch → extract → fingerprint, the fetch-command outbox, the revisions producer
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — logging configuration, ULID error handling, DB-trigger rules
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — systemd units, environment variables, migration ordering, wheelhouse auth
- [docs/SKILLS.md](docs/SKILLS.md) — skill triggers, vendored skill repos, SocratiCode workflow
- [docs/STYLE.md](docs/STYLE.md) — the design system: brand, color, dark mode, tokens, layout, touch targets, accessibility
- [docs/UI.md](docs/UI.md) — the component library (`.btn`, `.badge`, `.data-table`, …) and the HTMX/flash interaction patterns
- [docs/WATCHED-ITEMS.md](docs/WATCHED-ITEMS.md) — the WatchedItem entity: fields, schedule resolution, lifecycle guards, dashboard surfaces
