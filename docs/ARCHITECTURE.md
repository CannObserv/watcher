# Architecture

Module layout, the sibling-service topology, and the message-bus topology. The always-paid rules — single VM, single
process, port ownership — stay in `AGENTS.md`.

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

## Sibling services

Sibling services on the same VM, separately managed: **Archiver** (port 8020, `archiver.service`) and **Notifier**. Both are separate repos checked out alongside this one (`/home/exedev/archiver`, `/home/exedev/notifier` on this VM). Elsewhere in these docs they're named as "the Archiver repo" / "the Notifier repo" — resolve those against your own checkout.

**Archiver service.** Owns the canonical InfoItem / InfoSource / SourceRevision / RepSpec registry. Sibling repo (extracted in #149; see **Infrastructure** for checkout location). Watcher consumes it **over the bus only** — `info.registry` announcements reconciled into `watched_items` (#254) — and makes no HTTP calls to it at all; the `archiver-client` SDK and its path dependency were removed with the last one. Don't add Archiver code to this repo — go work in the sibling repo instead.

## Archiver checkout location

**The Archiver checkout location is not freely relocatable.** Two independent consumers resolve it, and only one takes an override:

- **Retired in #254.** `pyproject.toml` used to pin `archiver-client = { path = "../archiver/clients/python", editable = true }` — a relative path dependency that `uv sync` required and no env var could redirect, so a moved checkout broke the install while `ARCHIVER_REPO_PATH` kept the tests passing. Both halves are gone; `ARCHIVER_REPO_PATH` now locates the sibling repo for everything that still needs it (conftest's alembic run).
- `tests/conftest.py` reads `ARCHIVER_REPO_PATH` (default `/home/exedev/archiver`) to locate Archiver's alembic for the cross-schema test tables. This is the **only** reader — CI sets it in `.github/workflows/ci.yml`.

So `ARCHIVER_REPO_PATH` redirects the test harness alone. Setting it without also fixing the path dependency yields passing tests over a broken `uv sync`.

## Redis and the bus

**Redis and the bus (archiver#109, #245).** Archiver operates `redis-server` on this VM — the tracked drop-in, persistence and version-floor policy, and producer-side monitoring are all its; it also owns the `info.changes` fact stream. **Watcher publishes on four streams and consumes two.** Publish: `content.fetch` (commands, #241), `content.fetch-policy` (#245), `content.revisions` (`source_revision_observed` — the Archiver HTTP write path retired in #253), and `info.watch-status` (#264 — see below). Consume: `content.blobs` (fact stream, own consumer group) and `info.registry` (config/state stream, **groupless**). The politeness producer is the `content.fetch-policy` producer (#245; `src/core/fetch_policy.py` + the `publish_fetch_policy` periodic task) — Watcher's half of the cluster politeness split (*mechanism to Replicator, policy to the issuer, config over the bus*; normative: `docs/contracts/replicator-boundaries.md` in the Replicator repo). It publishes each `Domain.min_interval` (**never** `current_interval` — that column is inert 429-backoff state since the limiter retired) as a `FetchPolicyState` per host, full-set-republished every 5 minutes **including tombstones** (`fetch_policy_tombstones` table, written on domain delete, cleared on re-create) so a consumer's boot replay never depends on broker retention. Connection via `WATCHER_BUS_REDIS_URL` (unset → loud skip, Replicator falls back to its conservative default; `scripts/dev_server.sh` clears an inherited value unless `WATCHER_DEV_BUS_REDIS_URL` opts into a scratch bus). API domain routes defer an immediate republish; **dashboard routes deliberately don't** (they must not import `src.workers.*` — `tests/dashboard/test_import_decoupling.py`) and ride the periodic tick. Watcher joins exactly one consumer group — `watcher` on `content.blobs`, single-member (#241) — and all async work still stays on Procrastinate over Postgres (`PsycopgConnector`); the bus carries facts and commands, never jobs. **`info.registry` is the third stream kind and behaves differently (#254).** A config/state stream is broadcast like a fact but last-write-wins per key, so its consumer joins **no** group: it replays from `0-0` with `AsyncBusTailReader` at every boot and then tails. Reading from `$` is the mistake that fails silently — a booting worker sees nothing, indistinguishable from a worker whose registry is genuinely empty — and the driver offers no way to spell it. There is **no DLQ**: an undecodable frame is logged and `seek`-ed past (there is no ack to skip one with), and the producer's periodic snapshot supersedes whatever was dropped. Ordering comes from `generation`, not arrival: apply iff `generation >` the stored value, because the producer's outbox drain reorders under retry. See `src/workers/registry_reconcile.py`. **`info.watch-status` is the registry channel's return leg (#264, contract cannobserv#321): Watcher is the producer.** Same config/state posture in the opposite direction — broadcast LWW per `info_item_id`, no group, no DLQ, no outbox (a dropped frame is corrected by the next full set, which is why the republish period is the recovery bound; `WATCHER_WATCH_STATUS_REPUBLISH_CRON`, default 5 minutes). Payload is **levels, never edges** — `applied_generation` (0 = never reconciled; a real announcement is always ≥ 1 because archiver#141 bumps before every emit), `applied_active` (the conjunction the scheduler actually gates on), `applied_interval` (the resolved cadence **after** the throttle floor, so cadence-only drift is visible), `health` (`ok`/`error`/`unknown`), `last_attempt_at`/`last_observed_at`, and tombstones from `revoked_info_items`. Publishes fire on reconcile (post-commit — the publisher only ever reads committed rows, so `applied_generation` cannot travel early), on health/active/floor transitions, and on the periodic tick — **never per fetch cycle**; a steadily-healthy item costs one frame per period regardless of fetch rate. Archiver tails it into its `watch_status` table and renders the watched-item panel and announced-vs-applied drift detector from it (archiver#151). **It must never become an ack path**: nothing in Watcher blocks on Archiver reading it. See `src/core/watch_status.py` + `src/workers/watch_status.py`. Bus ownership design of record: `docs/plans/2026-07-29-redis-bus-ownership-design.md` in the Archiver repo ([on GitHub](https://github.com/CannObserv/archiver/blob/main/docs/plans/2026-07-29-redis-bus-ownership-design.md)).

## Phase 4 contracts

**Phase 4 contracts (#241) — done.** Watcher **is** the `content.fetch` issuer and `content.blobs` consumer; it makes no origin request of its own on any scheduled path (cut over 2026-08-06; `WATCHER_FETCH_MODE` and the inline-fetch branch deleted in step 5). The `fetch_commands` outbox/inbox, the issue path in `check_watched_item`, the single-member `content.blobs` consumer, the apply tasks, and the reaper are all documented in **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** — along with what step 5 retired, the inert `Domain` columns it left behind, and links to the two normative contracts in the Replicator repo (**link, don't copy**). Design: `docs/plans/2026-08-06-phase-4-content-fetch-producer-design.md`. #245 was the cutover's ordering blocker and shipped first.

## `info_source_id` on the wire

**`info_source_id` on the wire (#252, cannobserv#300).** Every `content.fetch` Watcher publishes names the Archiver InfoSource it is for; **correlation is unchanged** — `command_id` only (MUST-3), and an unmatched fact is still discarded. **Deploy ordering is load-bearing:** Replicator must ship its echo (replicator#28) to production *first*, and the migration has no safe order. Both, plus why the field is reporting and not routing: **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** and `docs/DEPLOYMENT.md` → "No safe order".

## Redis history and future use

Other *future* work that would widen Redis use: a Redis-backed aspect-review cache (#163). It does not exist. *History:* the Watcher-side `info.changes` publisher (`src/core/changes/`, `ChangePublisher`) was deleted in **#156** (Phase 5 cutover); the producer role migrated to Archiver (archiver#106), and archiver#109 assigned operational ownership.
