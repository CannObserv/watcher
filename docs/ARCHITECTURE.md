# Architecture

Module layout and the message-bus topology. The always-paid rules — single VM,
single process, port ownership — stay in `AGENTS.md`.

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

## Archiver checkout location

**The Archiver checkout location is not freely relocatable.** Two independent consumers resolve it, and only one takes an override:

- `pyproject.toml` → `[tool.uv.sources]` pins `archiver-client = { path = "../archiver/clients/python", editable = true }` — a **relative path dependency**. `uv sync` requires the repo at `../archiver` from this one, and honors no env var; moving it means editing that line.
- `tests/conftest.py` reads `ARCHIVER_REPO_PATH` (default `/home/exedev/archiver`) to locate Archiver's alembic for the cross-schema test tables. This is the **only** reader — CI sets it in `.github/workflows/ci.yml`.

So `ARCHIVER_REPO_PATH` redirects the test harness alone. Setting it without also fixing the path dependency yields passing tests over a broken `uv sync`.

## Redis and the bus

**Redis and the bus (archiver#109, #245).** Archiver operates `redis-server` on this VM — the tracked drop-in, persistence and version-floor policy, and producer-side monitoring are all its; it also owns the `info.changes` fact stream. **Watcher publishes on three streams and consumes one.** Publish: `content.fetch` (commands, #241), `content.fetch-policy` (#245), and `content.revisions` (`source_revision_observed` — the Archiver HTTP write path retired in #253). Consume: `content.blobs`. The politeness producer is the `content.fetch-policy` producer (#245; `src/core/fetch_policy.py` + the `publish_fetch_policy` periodic task) — Watcher's half of the cluster politeness split (*mechanism to Replicator, policy to the issuer, config over the bus*; normative: `docs/contracts/replicator-boundaries.md` in the Replicator repo). It publishes each `Domain.min_interval` (**never** `current_interval` — that column is inert 429-backoff state since the limiter retired) as a `FetchPolicyState` per host, full-set-republished every 5 minutes **including tombstones** (`fetch_policy_tombstones` table, written on domain delete, cleared on re-create) so a consumer's boot replay never depends on broker retention. Connection via `WATCHER_BUS_REDIS_URL` (unset → loud skip, Replicator falls back to its conservative default; `scripts/dev_server.sh` clears an inherited value unless `WATCHER_DEV_BUS_REDIS_URL` opts into a scratch bus). API domain routes defer an immediate republish; **dashboard routes deliberately don't** (they must not import `src.workers.*` — `tests/dashboard/test_import_decoupling.py`) and ride the periodic tick. Watcher joins exactly one consumer group — `watcher` on `content.blobs`, single-member (#241) — and all async work still stays on Procrastinate over Postgres (`PsycopgConnector`); the bus carries facts and commands, never jobs. Bus ownership design of record: `docs/plans/2026-07-29-redis-bus-ownership-design.md` in the Archiver repo ([on GitHub](https://github.com/CannObserv/archiver/blob/main/docs/plans/2026-07-29-redis-bus-ownership-design.md)).

## Phase 4 contracts

**Phase 4 contracts (#241) — done.** Watcher **is** the `content.fetch` issuer and `content.blobs` consumer; it makes no origin request of its own on any scheduled path (cut over 2026-08-06; `WATCHER_FETCH_MODE` and the inline-fetch branch deleted in step 5). The `fetch_commands` outbox/inbox, the issue path in `check_watched_item`, the single-member `content.blobs` consumer, the apply tasks, and the reaper are all documented in **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** — along with what step 5 retired, the inert `Domain` columns it left behind, and links to the two normative contracts in the Replicator repo (**link, don't copy**). Design: `docs/plans/2026-08-06-phase-4-content-fetch-producer-design.md`. #245 was the cutover's ordering blocker and shipped first.

## `info_source_id` on the wire

**`info_source_id` on the wire (#252, cannobserv#300).** Every `content.fetch` Watcher publishes names the Archiver InfoSource it is for; **correlation is unchanged** — `command_id` only (MUST-3), and an unmatched fact is still discarded. **Deploy ordering is load-bearing:** Replicator must ship its echo (replicator#28) to production *first*, and the migration has no safe order. Both, plus why the field is reporting and not routing: **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** and `docs/DEPLOYMENT.md` → "No safe order".

## Redis history and future use

Other *future* work that would widen Redis use: a Redis-backed aspect-review cache (#163). It does not exist. *History:* the Watcher-side `info.changes` publisher (`src/core/changes/`, `ChangePublisher`) was deleted in **#156** (Phase 5 cutover); the producer role migrated to Archiver (archiver#106), and archiver#109 assigned operational ownership.
