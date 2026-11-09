# Architecture

Module layout, the sibling-service topology (the bus is [BUS.md](BUS.md)'s), the
single-process constraint, and why nothing here mirrors to a sibling repo. The
always-paid rules — single VM, single
process, port ownership — stay in `AGENTS.md`; the reasoning behind them is here.

## Project Layout

Top-level directories. Read the code for per-file detail.

```
src/api/         FastAPI app: the lifespan that runs everything, and /health, /ready
src/core/        Shared domain logic (models, scheduling, fetch and process commands, the bus, watch facts)
src/workers/     Procrastinate task queue (check_watched_item, schedule_tick, pipeline, fetch apply/consumer, watch-check consumer)
src/ops/         Operator entry points beside the service: nightly backup, restore, backup check-in, job-history prune (#296), Archiver migration export (#373), break-glass host suspension (#372), the patching `inflight` gate (#382)
tools/           Operational scripts
tests/           Mirrors src/ structure
deploy/          Systemd units and deployment config
docs/            Reference docs (index: AGENTS.md → Detail Docs) + plans/
scripts/         Operator and dev scripts (dev server, env loading, co-pypi, pre-ship, cleanup)
skills/          Agent skills (committed overrides + symlinks → skills-vendor/)
skills-vendor/   Git submodules for external skill repos
.claude/skills/  Claude Code skill discovery (symlinks → ../../skills/<name>)
```

## Sibling services

Two sibling services, separately managed and neither co-located: **Archiver** runs on its own VM, `co-registrar` (`archiver.service` on port 8000 there, per archiver's own AGENTS.md), and watcher reaches it only through the broker; **Notifier** runs on its own VM too (notifier#43), and watcher reaches it **not at all** since #379 deleted the client: Archiver notifies from watcher's facts (#376). The broker is a MagicDNS name on the `cannobserv.org.github` tailnet, which this VM joined as node `watcher` / `tag:watcher` ([reference/tailscale.md](reference/tailscale.md) — identity, peers, the cold-boot race, the ACL rules).

Both are separate repos, and **nothing in this repo reads either checkout** — #311 removed the last reader (see *Archiver checkout location* below). The sibling directories exist for SocratiCode's `linkedProjects` alone: `/home/exedev/archiver` and `/home/exedev/notifier` are two-file *link stubs*, not clones — the notifier checkout stayed on the VM retired in #296, and the archiver clone was retired once #311 left nothing reading it. That is the expected state, not a missing dependency: cross-repo search answers from the shared store, not from source on disk ([SOCRATICODE.md](SOCRATICODE.md) § *Linked projects*). Elsewhere in these docs the two are named as "the Archiver repo" / "the Notifier repo" — resolve those against your own checkout.

**Archiver service.** Owns the canonical InfoItem / InfoSource / SourceRevision / RepSpec registry. Sibling repo (extracted in #149). Watcher consumes it **over the bus only** — `info.registry` announcements reconciled into `watched_items` (#254) — and makes no HTTP calls to it at all; the `archiver-client` SDK and its path dependency were removed with the last one. **Cadence is Archiver's since F2 (#375):** it resolves profile → item → domain, announces a concrete `interval`, and runs the temporal-profile boundary and its post-actions (CannObserv/archiver#364, #365); Watcher schedules on the announced interval, else its system default, and no longer reads its own item/domain cadence columns, throttle floor or `temporal_profiles` (kept, inert, until #380 — [WATCHED-ITEMS.md](WATCHED-ITEMS.md)). Don't add Archiver code to this repo — go work in the sibling repo instead.

## No cross-repo mirror discipline

**No cross-repo mirror discipline (#159, #236).** Content acquisition is co-core's (see [DEPENDENCIES.md](DEPENDENCIES.md)); `src/core/logging.py` is service-local. Nothing in `src/` needs mirroring to Archiver — don't reintroduce a sync obligation.

## Archiver checkout location

**Nothing in this repo reads the Archiver checkout (#311).** Two readers existed; both are gone:

- **Retired in #254.** `pyproject.toml` pinned `archiver-client = { path = "../archiver/clients/python", editable = true }` — a relative path dependency that `uv sync` required and no env var could redirect.
- **Retired in #311.** `tests/conftest.py` read `ARCHIVER_REPO_PATH` (default `/home/exedev/archiver`) to subprocess-run Archiver's alembic and build an `information` schema in the test database — a schema production has not had since #271, whose rows the factories wrote only to mint two ULIDs. The WatchedItem links are bare ULIDs with no FK behind them, so `make_watched_item` mints them directly, and `tests/test_archiver_isolation.py` fails if the test database carries the schema again. CI checks out no sibling.

A test that seems to need a real Archiver row is testing Archiver. Exercise the contract at the bus boundary instead — `tests/test_renewal_wire_contract.py`, the `info.registry` reconcile tests.

## Single process

**Single process is load-bearing.** One uvicorn process runs everything: `/health` and `/ready`, the embedded Procrastinate worker, the `content.blobs` and `content.derived` fact consumers, the `info.watch-check` command consumer (#371), and the cache sweeper (started in the `src/api/main.py` lifespan — there is no separate worker unit). The reason is now the **fact consumer**, not politeness: `src/workers/fetch_facts.py` joins consumer group `watcher.blobs` as a single member (`watcher-blobs-1`; `watcher.derived` likewise, #325), and a second process would need its own consumer name *and* an apply-ordering story across members — the supersession guard is per-row, not a cross-process lock. (Until #241 step 5 the reason was the in-process `DomainRateLimiter`, retired with the local fetch path; per-host pacing is Replicator's now, fed over `content.fetch-policy`, and no longer constrains the topology.) Worker restarts and stop bound: `src/workers/supervisor.py` (#340, #334). Never run `uvicorn --workers N` or a second worker unit against prod. Escalation path, only once one process is not enough: a separate `watcher-worker.service` plus a multi-member consumer-group design — **not built**.

## Probe destination guard (retired)

The URL probe (`src/core/probe.py`) and its destination guard (`src/core/egress.py`, #305, #402) went with `/api/v1`: since #377 the API's `POST /api/v1/probe` was their only caller. Nothing in Watcher takes an operator-typed URL any more, and Watcher makes no origin request itself (#241). The guard's design and its replicator-derived predicate are in git history before #378.

## Redis and the bus

**Moved to [BUS.md](BUS.md)** — broker ownership, the stream inventory and retention, the connection policy, Redis history.

## Phase 4 contracts

**Phase 4 contracts (#241) — done.** Watcher **is** the `content.fetch` issuer and `content.blobs` consumer; it makes no origin request of its own on any scheduled path (cut over 2026-08-06; `WATCHER_FETCH_MODE` and the inline-fetch branch deleted in step 5). The `fetch_commands` outbox/inbox, the issue path in `check_watched_item`, the single-member `content.blobs` consumer, the apply tasks, and the reaper are all documented in **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** — along with what step 5 retired, the inert `Domain` columns it left behind, and links to the two normative contracts in the Replicator repo (**link, don't copy**). Design: `docs/plans/2026-08-06-phase-4-content-fetch-producer-design.md`. #245 was the cutover's ordering blocker and shipped first.

## `info_source_id` on the wire

**`info_source_id` on the wire (#252, cannobserv#300).** Every `content.fetch` Watcher publishes names the Archiver InfoSource it is for; **correlation is unchanged** — `command_id` only (MUST-3), and an unmatched fact is still discarded. **Deploy ordering is load-bearing:** Replicator must ship its echo (replicator#28) to production *first*, and the migration has no safe order. Both, plus why the field is reporting and not routing: **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** and `docs/MIGRATIONS.md` → "No safe order".
