# Architecture

Module layout, the sibling-service topology (the bus is [BUS.md](BUS.md)'s), the
single-process constraint, and why nothing here mirrors to a sibling repo. The
always-paid rules — single VM, single
process, port ownership — stay in `AGENTS.md`; the reasoning behind them is here.

## Project Layout

Top-level directories. Read the code for per-file detail.

```
src/api/         FastAPI app (ASGI routes, schemas, deps)
src/core/        Shared domain logic (models, probe, scheduling, notifications, diff, fetch commands, storage, crypto)
src/dashboard/   Server-rendered UI (Jinja2 + HTMX + Tailwind)
src/workers/     Procrastinate task queue (check_watched_item, schedule_tick, pipeline, fetch apply/consumer)
src/ops/         Operator entry points beside the service: nightly backup, restore, backup check-in, job-history prune (#296)
tools/           Operational scripts
tests/           Mirrors src/ structure
deploy/          Systemd units and deployment config
docs/            Reference docs (index: AGENTS.md → Detail Docs) + plans/
scripts/         Build scripts (Tailwind, vendor CSS, cleanup)
skills/          Agent skills (committed overrides + symlinks → skills-vendor/)
skills-vendor/   Git submodules for external skill repos
.claude/skills/  Claude Code skill discovery (symlinks → ../../skills/<name>)
```

## Sibling services

Two sibling services, separately managed and neither co-located: **Archiver** runs on its own VM, `co-registrar` (`archiver.service` on port 8000 there, per archiver's own AGENTS.md), and watcher reaches it only through the broker; **Notifier** moved to a dedicated VM under notifier#43 (#280) — reached as `http://notifier:9000`, a MagicDNS name on the `cannobserv.org.github` tailnet, which this VM joined as node `watcher` / `tag:watcher` ([reference/tailscale.md](reference/tailscale.md) — identity, peers, the cold-boot race, the ACL rules). Notifier binds its tailnet address alone, so a watcher process that is not on the tailnet cannot reach it from loopback, from exe.dev's internal `10.42.0.0/16`, or from the internet. That is a property of notifier's own launch path rather than a firewall: both its units run `scripts/serve.sh` / `scripts/dev_server.sh`, which resolve this host's `100.x` address through `scripts/tailnet_bind.sh` and pass it as uvicorn's single `--host`, blocking until tailscaled assigns one and failing loudly rather than falling back to a bind reachable from anywhere else (observo#473/#479). Strictly a default, not an invariant — `tailnet_bind.sh` honours a `NOTIFIER_BIND_HOST` override — but nothing on this side can set it, and both deployed units take the tailnet address.

Both are separate repos, and **nothing in this repo reads either checkout** — #311 removed the last reader (see *Archiver checkout location* below). The sibling directories exist for SocratiCode's `linkedProjects` alone: `/home/exedev/archiver` and `/home/exedev/notifier` are two-file *link stubs*, not clones — the notifier checkout stayed on the VM retired in #296, and the archiver clone was retired once #311 left nothing reading it. That is the expected state, not a missing dependency: cross-repo search answers from the shared store, not from source on disk ([SOCRATICODE.md](SOCRATICODE.md) § *Linked projects*). Elsewhere in these docs the two are named as "the Archiver repo" / "the Notifier repo" — resolve those against your own checkout.

**Archiver service.** Owns the canonical InfoItem / InfoSource / SourceRevision / RepSpec registry. Sibling repo (extracted in #149). Watcher consumes it **over the bus only** — `info.registry` announcements reconciled into `watched_items` (#254) — and makes no HTTP calls to it at all; the `archiver-client` SDK and its path dependency were removed with the last one. Don't add Archiver code to this repo — go work in the sibling repo instead.

## No cross-repo mirror discipline

**No cross-repo mirror discipline (#159, #236).** Content acquisition is co-core's (see [DEPENDENCIES.md](DEPENDENCIES.md)); `src/core/logging.py` is service-local. Nothing in `src/` needs mirroring to Archiver — don't reintroduce a sync obligation.

## Archiver checkout location

**Nothing in this repo reads the Archiver checkout (#311).** Two readers existed; both are gone:

- **Retired in #254.** `pyproject.toml` pinned `archiver-client = { path = "../archiver/clients/python", editable = true }` — a relative path dependency that `uv sync` required and no env var could redirect.
- **Retired in #311.** `tests/conftest.py` read `ARCHIVER_REPO_PATH` (default `/home/exedev/archiver`) to subprocess-run Archiver's alembic and build an `information` schema in the test database — a schema production has not had since #271, whose rows the factories wrote only to mint two ULIDs. The WatchedItem links are bare ULIDs with no FK behind them, so `make_watched_item` mints them directly, and `tests/test_archiver_isolation.py` fails if the test database carries the schema again. CI checks out no sibling.

A test that seems to need a real Archiver row is testing Archiver. Exercise the contract at the bus boundary instead — `tests/test_renewal_wire_contract.py`, the `info.registry` reconcile tests.

## Single process

**Single process is load-bearing.** One uvicorn process runs everything: the API, the embedded Procrastinate worker, the `content.blobs` and `content.derived` fact consumers, and the cache sweeper (started in the `src/api/main.py` lifespan — there is no separate worker unit). The reason is now the **fact consumer**, not politeness: `src/workers/fetch_facts.py` joins consumer group `watcher.blobs` as a single member (`watcher-blobs-1`; `watcher.derived` likewise, #325), and a second process would need its own consumer name *and* an apply-ordering story across members — the supersession guard is per-row, not a cross-process lock. (Until #241 step 5 the reason was the in-process `DomainRateLimiter`, retired with the local fetch path; per-host pacing is Replicator's now, fed over `content.fetch-policy`, and no longer constrains the topology.) Worker restarts and stop bound: `src/workers/supervisor.py` (#340, #334). Never run `uvicorn --workers N` or a second worker unit against prod. Escalation path, only once one process is not enough: a separate `watcher-worker.service` plus a multi-member consumer-group design — **not built**.

## Probe destination guard

**A probe may not reach this host, its private network, or the tailnet (#305).** `probe_url` (`src/core/probe.py`) takes a URL an operator typed and issues `HEAD`, returning status code, content type and the full redirect chain — so the capability is not "issue an outbound request" but *reach whatever this host's network position reaches, and report back*. Both call sites are authenticated (`require_api_key` on `POST /api/v1/probe`, `get_dashboard_user` on `POST /domains`), which bounds who may pull the trigger and is why this was never urgent; nothing bounded where the barrel pointed, and on co-watcher there is something to reach — `127.0.0.1:9999` answers 200 unauthenticated (exeuntu's socket-activated Shelley agent UI, which the exe.dev proxy authenticates and loopback bypasses; #304, CannObserv/replicator#97). `src/core/watched_items.py` validates scheme and hostname *presence*, which is syntactic and stays that way.

**The guard is a transport, not a check on the submitted URL.** `follow_redirects=True` means the submitted URL does not decide the destination: a public origin answering `302 Location: http://127.0.0.1:9999/` walks around anything applied to the string the operator typed, so a top-of-function check is close to no guard at all. `GuardedTransport` (`src/core/egress.py`, composed in by `build_probe_client`) resolves and checks **every** address of **every** hop before the request reaches the inner transport, and refuses with `DestinationRefused` — deliberately **not** an `httpx.HTTPError`, so the two routes' existing "unreachable" branches cannot report a refusal as a URL that could not be reached. `POST /api/v1/probe` returns its own 422 (`Destination refused: …`); the domain-create form flashes its own message. The guard owns two bounds httpx used to provide, both because a check ahead of the inner transport is ahead of httpx's own machinery: the resolve has its own deadline (`RESOLVE_TIMEOUT`, since httpcore wraps name resolution in the *connect* timeout and nothing wrapped this one; 10 s leaves room for glibc's second 5 s try after a dropped packet, #316), and **environment proxies are not honoured for probes** — httpx builds its proxy map only when it builds the transport itself (`allow_env_proxies = trust_env and transport is None`), so an `HTTPS_PROXY` in `/etc/watcher/.env` would reach everything in this service except here. None is set today; `AsyncHTTPTransport` keeps `trust_env=True`, so SSL-env handling is unaffected. Expected refusal count in normal operation is zero — not because of the watched-item set (nothing probes watched items since #241) but because a probe is only ever an operator typing a URL for a site they intend to watch, and those are public.

**The predicate is replicator's, copied rather than re-derived.** The range table (loopback, RFC 1918, link-local, ULA, CGNAT `100.64.0.0/10`, unspecified, multicast, reserved), the IPv4-mapped normalisation, the literals-are-never-resolved rule and the boundary parse (`_checkable`, #316) come from `src/worker/egress.py` in CannObserv/replicator (their #95 and #100, decision in their #89), which closed the same hole on the `content.fetch` path. **An answer the guard cannot check — empty in any shape, or not an address — is a `ConnectError`, never a pass**: the check is a loop, and a loop over nothing refuses nothing. `co-core` carries no address predicate, so it could not be imported; the module docstring names the source so the two can be diffed. Two deliberate divergences, both because this is a one-shot operator probe rather than a retrying worker loop: watcher's table is **compiled** where replicator's is carried in `REPLICATOR_BLOCKED_DESTINATIONS` (no configuration, therefore no env file whose absence quietly empties it), and a resolution failure is re-raised as `httpx.ConnectError` rather than split into transient/permanent — resolving ahead of httpx moves where a name failure surfaces, and both routes classify by exception type. **DNS rebinding is a stated residual, not a closed one**: the check resolves and then hands the *name* to the inner transport, which resolves again. Closing that window means connecting to the pinned address with the `Host` header and certificate verification still keyed to the name, which is more machinery than a threat needing both a hostile origin and an authenticated operator aiming a probe at it.

## Redis and the bus

**Moved to [BUS.md](BUS.md)** — broker ownership, the stream inventory and retention, the connection policy, Redis history.

## Phase 4 contracts

**Phase 4 contracts (#241) — done.** Watcher **is** the `content.fetch` issuer and `content.blobs` consumer; it makes no origin request of its own on any scheduled path (cut over 2026-08-06; `WATCHER_FETCH_MODE` and the inline-fetch branch deleted in step 5). The `fetch_commands` outbox/inbox, the issue path in `check_watched_item`, the single-member `content.blobs` consumer, the apply tasks, and the reaper are all documented in **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** — along with what step 5 retired, the inert `Domain` columns it left behind, and links to the two normative contracts in the Replicator repo (**link, don't copy**). Design: `docs/plans/2026-08-06-phase-4-content-fetch-producer-design.md`. #245 was the cutover's ordering blocker and shipped first.

**Suspended for any reason ⇒ no live fetch policy published (#250).** A `Domain` that is **archived** (`archived_at IS NOT NULL`) or **deactivated** (`is_active = false`) publishes `revoked=True` with no `min_interval_seconds` on `content.fetch-policy`, not its live interval — both states already suspend every WatchedItem on the domain, so a live policy would assert configuration Watcher no longer acts on. This is safe rather than merely tidy because of what `revoked` means in the contract (`FetchPolicyState`, cannobserv#285): it is the tombstone LWW has no delete for, and it says "**no explicit policy for this host**", *not* "no limit" — the consumer falls back to its own default, which rule 1 requires be **at least as strict as anything the producer would publish**. Revocation therefore cannot open a politeness gap, while republishing a live `min_interval` for a host Watcher has stopped watching is the one way that host ends up *looser* than the fallback. Rule 2 ("keep republishing revoked hosts") costs nothing here: the row survives archiving, so the domain keeps appearing in every full set — the producer query stays unfiltered and only what it *emits* changes (`is_suspended` / `build_policy_events`, `src/core/fetch_policy.py`). Restore and reactivate need no operator action and no tombstone bookkeeping: the next full set reads the cleared columns and emits live again, and revoked → live is an ordinary LWW overwrite on the `host` field. Deleting a domain is still the separate path — the `fetch_policy_tombstones` table, which exists only to carry the obligation past a row that no longer exists.

## `info_source_id` on the wire

**`info_source_id` on the wire (#252, cannobserv#300).** Every `content.fetch` Watcher publishes names the Archiver InfoSource it is for; **correlation is unchanged** — `command_id` only (MUST-3), and an unmatched fact is still discarded. **Deploy ordering is load-bearing:** Replicator must ship its echo (replicator#28) to production *first*, and the migration has no safe order. Both, plus why the field is reporting and not routing: **[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)** and `docs/MIGRATIONS.md` → "No safe order".
