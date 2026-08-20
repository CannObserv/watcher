# Deployment

## Environment Files

The service loads env files in this order (later values override earlier):

| File | Purpose | Required |
|---|---|---|
| `/run/watcher/build-id` | `BUILD_ID` (auto-generated from git SHA) | optional |
| `/etc/watcher/.env` | Production secrets (`DATABASE_URL`) | **yes** |
| `.env` (repo root) | Dev/agent overrides (`GH_TOKEN`, `TEST_DATABASE_URL`) | optional |

`/etc/watcher/.env` is owned by `root:exedev` (mode 640) and survives repo resets, worktree switches, and redeployments.

For shell commands that need secrets:

```bash
source scripts/load-env.sh
```

## Environment Variables

**Naming rule for new variables.** Anything naming a shared external resource
takes a **service-prefixed** name with a separate dev key — Archiver's
`ARCHIVER_REDIS_URL` / `ARCHIVER_DEV_REDIS_URL` split is the pattern. A bare
unprefixed name (`REDIS_URL`) is silently inherited from `/etc/watcher/.env` by
anything that sources it, which is exactly how a dev process ends up pointed at
a production resource (the #233 hazard, in env-var form). Watcher's own
`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL` split (#245) follows the
pattern — see **Redis and the bus**.

| Variable | Location | Required | Purpose |
|---|---|---|---|
| `DATABASE_URL` | `/etc/watcher/.env` | **yes** | PostgreSQL connection string the **application** connects with. Once the role split is applied (below) this names `watcher_app`, which holds DML and no DDL |
| `WATCHER_MIGRATION_DATABASE_URL` | `/etc/watcher/.env` | no | Connection string **Alembic** connects with — the schema owner, `watcher` (#259). Unset → falls back to `DATABASE_URL`, which is the pre-split behaviour and what every host uses until `scripts/setup-db-roles.sql` has run. Set-but-empty counts as unset — which is what the shell launchers rely on; the service has it removed outright. `scripts/dev_server.sh` overwrites it with the dev database, and `tests/conftest.py` pins it to `TEST_DATABASE_URL`: it is the one variable that can drop tables, so it is never inherited by a non-production launch path. `deploy/watcher.service` drops it from the service process with `UnsetEnvironment=` (#270) — only `alembic/env.py` ever reads it |
| `PROCRASTINATE_DATABASE_URL` | `/etc/watcher/.env` | no | libpq-style DSN for procrastinate; falls back to `DATABASE_URL` with driver prefix stripped |
| `GH_TOKEN` | `.env` | no | GitHub personal access token |
| `TEST_DATABASE_URL` | `.env` | no | PostgreSQL connection string for test database |
| `BUILD_ID` | env | no | Git SHA for static asset cache-busting (default `"dev"`) |
| `NOTIFIER_BASE_URL` | `/etc/watcher/.env` | **yes** | Base URL of the notifier service (e.g. `http://localhost:9000`) |
| `NOTIFIER_API_KEY` | `/etc/watcher/.env` | **yes** | Watcher tenant API key issued by `scripts/seed_tenant.py` in the notifier repo |
| `WATCHER_ALLOW_PRODUCTION_DB` | `deploy/watcher.service` **only** | prod only | `=1` opts into serving a database whose name lacks a `_test`/`_dev` suffix (`src/core/db_safety.py`, #233). Must live in the systemd unit, never an env file — env files are sourced by hand-run dev servers, which are exactly what the guard stops |
| `WATCHER_DEV_DATABASE_URL` | `.env` | no | Persistent dev database for `scripts/dev_server.sh`; wins over `TEST_DATABASE_URL` |
| `WATCHER_BUS_REDIS_URL` | `/etc/watcher/.env` | prod | Redis URL of the Archiver-operated broker (`redis://localhost:6379/0`) for the `content.fetch-policy` and `info.watch-status` producers (#245, #264). Unset → both periodic publish tasks skip with an ERROR log: Replicator paces every host at its own conservative default, and Archiver's watched-item panel / drift detector go stale. **Not sufficient on its own since #262** — see `WATCHER_BUS_ENABLED` |
| `WATCHER_BUS_ENABLED` | `deploy/watcher.service` **only** | prod only | `=1` opts this process into building a bus client at all (`src/core/bus.py`, #262). Without it `bus_client_from_env()` returns `None`, so nothing publishes and neither consumer starts — **and a URL held without it aborts startup** with `BusNotEnabled`, so a unit that lost the line fails loudly instead of going quiet. Must live in the systemd unit, never an env file: `WATCHER_BUS_REDIS_URL` does live in one, so every process that sources `/etc/watcher/.env` inherits the production broker address, and the flag is the only thing separating the service from an agent shell or a REPL. `scripts/dev_server.sh` sets it for itself when `WATCHER_DEV_BUS_REDIS_URL` names a scratch bus |
| `WATCHER_WATCH_STATUS_REPUBLISH_CRON` | env | no | Cron expression for the `info.watch-status` full-set republish (default `*/5 * * * *`, #264). The period is the recovery bound for a dropped frame — this stream has no outbox by design; loss is corrected by the next full set. A malformed value falls back to the default with an ERROR log |
| `WATCHER_WATCH_STATUS_STREAM_MAXLEN` | env | no | Producer-enforced retention cap for `info.watch-status` (`XADD MAXLEN ~`, default `50000`). The full set republishes forever, so an untrimmed stream grows without bound; invalid/non-positive values fall back to the default with a warning — never to unbounded |
| `WATCHER_FETCH_POLICY_STREAM_MAXLEN` | env | no | Same producer-enforced retention cap for `content.fetch-policy` (default `50000`) |
| `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS` | env | no | Reaper timeout for an in-flight command with no fact (default `1800` — deliberately generous; Replicator's reclaim cadence is an operator knob, and a tight value re-issues under live retries) |
| `WATCHER_CONDITIONAL_GET_ENABLED` | env | no | Which items may replay stored conditional-GET validators (#269). Unset/falsey → off for everything and every command is byte-identical to the pre-#269 one; `true` → the whole fleet; anything else is read as a comma-separated list of WatchedItem ids, which is the canary position. Safe only while replicator#17 and #249 part 1 are both deployed — a validator sent to a Replicator that classifies 304 as a plain fetch failure marks a healthy item ERROR and notifies a user about it on every no-change check |
| `WATCHER_VALIDATOR_MAX_AGE_HOURS` | env | no | How long a stored validator pair may be replayed before one unconditional re-fetch is forced (default `168`). The residual net under the deterministic invalidation rules — an origin whose ETag tracks a template rather than the watched region would otherwise hold a fingerprint inherited indefinitely. An unparseable value falls back to the default rather than raising, and a zero or negative value disables replay entirely (logged at INFO): the knob must not be able to wedge the issue path |
| `GCS_BLOB_CREDENTIALS` | env | for `gs://` blobs | Key file for the `co-gcs-blob-reader` SA (`/etc/watcher/co-gcs-blob-reader.json`), read by the `gs://` blob arm (#275). Singular `BLOB` — easy to typo as `BLOBS`. Deliberately **not** `GOOGLE_APPLICATION_CREDENTIALS`, which is the wheelhouse identity: reading fetched content and reading the private package index are different jobs and must not share a principal. Unset → every `gs://` blob fails permanently (`blob_unreadable`, no re-issues) until set |
| `WATCHER_FETCH_MAX_REISSUES` | env | no | Re-issues per fetch intent before it fails with ERROR health (default `3`). Caps the *lineage*, not one path: both the reaper's stall sweep and the blob-unreadable apply (#275) read the same `reissue_count` |
| `WATCHER_DEV_BUS_REDIS_URL` | `.env` | no | Scratch-bus opt-in for `scripts/dev_server.sh`; without it the dev server **clears** an inherited `WATCHER_BUS_REDIS_URL` (and `WATCHER_BUS_ENABLED`) so it cannot publish policy onto the production stream. With it, the script exports both, since the flag is otherwise unit-only |

**Watcher's Redis use.** Archiver operates `redis-server` and owns the broker
(archiver#109). Watcher publishes `content.fetch-policy` (#245) and — Phase 4,
#241 — publishes `content.fetch` commands and consumes `content.blobs` facts
via its own consumer group (`watcher`, started in the lifespan when
`WATCHER_BUS_REDIS_URL` is set **and** `WATCHER_BUS_ENABLED=1`, #262). Since
#254 it also consumes `info.registry` **grouplessly**, replayed from `0-0` at
boot; without a usable bus neither consumer starts and the registry cannot
converge. Since #264 it publishes
`info.watch-status` — the return leg of the registry channel: applied
generation, scheduler state, and observation freshness per InfoItem, full-set
republished on `WATCHER_WATCH_STATUS_REPUBLISH_CRON` (default every 5
minutes) including tombstones from `revoked_info_items`. **Health primitive:
last-entry age** — `redis-cli XREVRANGE info.watch-status + - COUNT 1` should
never be older than the republish period while the service is up; an aging
stream with a live service means the publish task is failing (check
Procrastinate job errors), and Archiver's panel renders drift from exactly
this staleness. All queued work stays on Procrastinate over
Postgres. See [ARCHITECTURE.md](ARCHITECTURE.md) § *Redis and the bus* for the ownership split.

## Systemd Service

A systemd unit file is provided at `deploy/watcher.service`.

### Installation

> **Install the Archiver service first** — see [§ Archiver Service](#archiver-service) below. Watcher no longer holds an Archiver SDK and will boot without one (#254), but it consumes `info.registry` off the Archiver-operated broker, so until that is up `watched_items` cannot reconcile and no registry state arrives.

```bash
# Create system env directory
sudo mkdir -p /etc/watcher
# Add production secrets (at minimum DATABASE_URL)
echo 'DATABASE_URL=postgresql+asyncpg://watcher:watcher@localhost:5432/watcher' | sudo tee /etc/watcher/.env
sudo chmod 640 /etc/watcher/.env
sudo chown root:exedev /etc/watcher/.env

# IMPORTANT: /etc/watcher/.env must exist before starting the service.
# Without it, systemd will refuse to start the unit (EnvironmentFile is required).

# Copy (or symlink) the unit file
sudo cp deploy/watcher.service /etc/systemd/system/watcher.service

# Reload systemd, enable, and start
sudo systemctl daemon-reload
sudo systemctl enable watcher
sudo systemctl start watcher
```

### Managing the Service

```bash
# Restart after code changes
sudo systemctl restart watcher

# Check status
sudo systemctl status watcher

# Follow logs
sudo journalctl -u watcher -f

# Follow WARNING+ only (app records are JSON: timestamp/level/logger/message — #238).
# `-R` + `fromjson?` is load-bearing: the ExecStartPre wheelhouse sync writes a
# plain-text line on every start (#247), and bare `jq` aborts on it with a parse
# error — killing the follow and hiding every record after it. Non-JSON lines are
# tagged PLAIN rather than dropped, so a failed sync still surfaces here.
# Needs jq (present on this VM at /bin/jq); the grep below is the no-jq fallback.
sudo journalctl -u watcher -f -o cat | jq -R 'fromjson? // {level: "PLAIN", message: .} | select(.level | IN("WARNING","ERROR","CRITICAL","PLAIN"))'
sudo journalctl -u watcher -f -o cat | grep -E '"level": "(WARNING|ERROR|CRITICAL)"|^error: '

# Reload after editing deploy/watcher.service
sudo systemctl daemon-reload && sudo systemctl restart watcher
```

Every line the **application** writes is JSON, uvicorn's included: the unit's
`ExecStart` passes `--log-config src/core/log_config.json`, which routes uvicorn's
own `uvicorn`/`uvicorn.access`/`uvicorn.error` loggers (`propagate=False`,
plain-text handlers by default) through the app's `build_json_formatter()` (#244),
and strips uvicorn's ANSI `color_message` duplicate from the payload (#246). Drop
the flag and the `jq` filter above silently skips the access/boot lines, which come
back as plain text. Same flag is baked into `scripts/dev_server.sh`.

The unit's `ExecStartPre` steps are the exception (#247): they run before the
project is importable, so their output is plain text by design — the wheelhouse
sync writes `wheelhouse in sync: …` on every start and `error: could not sync
gs://…` when it fails, and the BUILD_ID stamp writes plain text on failure. Any
pipeline that `json.loads` every `MESSAGE` must tolerate them; reading journald's
own fields (`_SYSTEMD_UNIT`, `SYSLOG_IDENTIFIER`, `MESSAGE`) is unaffected. See
[CONVENTIONS.md](CONVENTIONS.md) → *`ExecStartPre` output is plain text*.

## Database Migrations

Split out to [MIGRATIONS.md](MIGRATIONS.md) — the manual `alembic upgrade head`
step, the two-role grant model (#259), the autogenerate scratch-database rule,
and the `information`-schema drop (#234).

## Archiver Service

The Archiver is a sibling service (port 8020, `archiver.service`; its repo is
checked out alongside this one — `/home/exedev/archiver` on this VM, located for the
test harness by `ARCHIVER_REPO_PATH`) that owns the canonical InfoItem / InfoSource /
SourceRevision / RepSpec registry.

**Watcher makes no HTTP calls to it.** The SDK, its API key, and the lifespan pre-warm
were removed in #254 together with the last outbound call (`get_info_item` on WatchedItem
create); registry state now arrives as `info.registry` announcements, which Watcher
reconciles into `watched_items`. `watcher.service` therefore boots regardless of
Archiver's state — but with the broker down or the producer not running, the registry
simply never converges, and the log line to look for is the `info.registry` consumer
failing to start.

See the Archiver repo's `docs/DEPLOYMENT.md` for the full Archiver install
(key generation, env-var registration, systemd unit). After installing
`archiver.service`, restart `watcher.service`:

```bash
sudo systemctl restart watcher
```

## Archiver Sync

Every detected change enqueues a `pending_archiver_sync` row; the
`drain_pending_archiver_sync` periodic task
(`src/workers/source_revisions_drain.py`) publishes each as
`source_revision_observed` on `content.revisions`, on a fixed **1-minute**
cadence — a hardcoded Procrastinate `cron`, not an env var. Nothing is POSTed to
Archiver any more (#253); Archiver consumes the stream and decides what to
persist. A broker outage self-heals within a minute of Redis returning, and the
outbox holds the backlog meanwhile.

With `WATCHER_BUS_REDIS_URL` unset the drain skips loudly and rows accumulate
rather than draining — the same "no bus, no publish" posture as the fetch-policy
producer, and the reason an unset broker URL shows up as a growing backlog.

The drain runs on the embedded worker inside the single uvicorn process; there
is no separate unit to start or monitor.

**There is no scratch cache any more (#253).** Watcher used to write its own
copy of the extracted bytes, report *that* path as `content_cache_uri`, sweep it,
and PATCH null — three moving parts doing nothing Replicator's `blob_uri` does.
The copy, the sweeper, and the `WATCHER_CACHE_*` variables are gone; a stuck
outbox now shows up only as backlog rows, never as disk growth.

**A dead-lettered row is the one thing the backlog query can miss.** A row whose
payload cannot be built (a missing wire-required field) is stamped
`dead_lettered_at` and stops being selected — deliberately, so it cannot spin
forever. It will sit in the table indefinitely, so count it separately rather
than reading a flat backlog number as "the drain is fine".

To spot one:

```bash
source scripts/load-env.sh
# Backlog size, age of the oldest undrained row, and the last failure reason.
# psql needs a driverless URL — strip the SQLAlchemy "+asyncpg" dialect suffix.
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT count(*), min(created_at) AS oldest, max(attempts) AS max_attempts FROM pending_archiver_sync;"
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT id, attempts, last_error FROM pending_archiver_sync ORDER BY created_at LIMIT 5;"
# Rows that will never drain on their own — these need an operator, not patience.
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT id, dead_lettered_at, last_error FROM pending_archiver_sync
   WHERE dead_lettered_at IS NOT NULL ORDER BY dead_lettered_at DESC LIMIT 10;"
```

A non-empty backlog with an oldest row older than a few minutes means the drain
is failing, not merely busy — check `journalctl -u watcher -f` for
`drain attempt failed` and confirm Archiver is reachable.

## BUILD_ID

The systemd unit automatically sets `BUILD_ID` to the current git short SHA before each start via `ExecStartPre`. This value is used for:

- Static asset cache-busting (`?v=<sha>` query params)
- Page footer version display
- `/health` endpoint `build` field

### Manual Override

To pin a specific build ID, set it in `.env`:

```bash
echo 'BUILD_ID=abc1234' >> .env
```

`.env` is loaded after `/etc/watcher/.env` and `/run/watcher/build-id` (both use the `-` prefix to be optional), so values in `.env` take precedence.

### Fallback

When `BUILD_ID` is not set (e.g., running locally without the systemd unit), the application defaults to `"dev"`.

## Disk Cleanup Timer

A weekly cleanup timer removes stale caches and rotates journal logs to keep the 19 GB VM disk healthy.

Files:
- `deploy/watcher-cleanup.service` — oneshot service, runs as `exedev`
- `deploy/watcher-cleanup.timer` — fires Sun 03:00 UTC ± 30 min
- `deploy/watcher-cleanup.sudoers` — two targeted NOPASSWD rules (`apt-get clean`, `journalctl --vacuum-time=14d`)
- `scripts/cleanup.sh` — the cleanup script; logs to `/var/log/watcher/cleanup-<timestamp>.log` (keeps 10)

### Installing the timer

```bash
# Sudoers rules
sudo cp deploy/watcher-cleanup.sudoers /etc/sudoers.d/watcher-cleanup
sudo chmod 440 /etc/sudoers.d/watcher-cleanup
sudo chown root:root /etc/sudoers.d/watcher-cleanup
sudo visudo -c  # verify no syntax errors

# Make the script executable
chmod +x scripts/cleanup.sh

# Install and enable the systemd units
sudo cp deploy/watcher-cleanup.service /etc/systemd/system/watcher-cleanup.service
sudo cp deploy/watcher-cleanup.timer   /etc/systemd/system/watcher-cleanup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now watcher-cleanup.timer
```

### Managing the Timer

```bash
# Confirm it is scheduled
systemctl list-timers watcher-cleanup.timer

# Manual run (for testing)
sudo systemctl start watcher-cleanup.service

# Follow output
sudo journalctl -u watcher-cleanup.service -f

# View logs
ls -lt /var/log/watcher/cleanup-*.log
cat "$(ls -t /var/log/watcher/cleanup-*.log | head -1)"

# Reload after editing the timer
sudo systemctl daemon-reload && sudo systemctl restart watcher-cleanup.timer
```

### What it cleans

| Target | Action |
|---|---|
| VS Code server installs >30 days old | `rm -rf` |
| `~/.npm/_npx`, `~/.npm/_cacache` | `rm -rf` |
| uv build cache | `uv cache prune` |
| APT package cache | `apt-get clean` |
| Docker dangling images | `docker image prune -f` |
| Journal logs >14 days | `journalctl --vacuum-time=14d` |
| Playwright cache | audit only — logs size, warns if >2 GB, never deletes |

## Cannobserv wheelhouse

**Cannobserv wheelhouse (#220).** `co-core` + `co-core-aio` (the shared cannabis-observer substrate) resolve from a local wheelhouse mirrored from the private GCS index `gs://co-gcs-pypi`, via `[tool.uv] find-links = ["./.wheelhouse"]` — **not** git sources. Populate it **before any `uv` command** (find-links makes every `uv` invocation require the dir; `.wheelhouse/.gitkeep` is tracked so a fresh clone has it):

Auth is ADC: on the VM/deploy the `co-pypi-reader` SA key at `GOOGLE_APPLICATION_CREDENTIALS` (in `/etc/watcher/.env`); in CI, keyless via Workload Identity Federation (`.github/workflows/ci.yml`). The identity needs only `roles/storage.objectViewer`. Reproducibility is `uv.lock` (pins the exact version), not wheelhouse contents. **Upgrade:** re-sync, then `uv lock --upgrade-package co-core` (bump the floor if the minor moved). Currently pinned: **v0.10.0** (floors `>=0.10,<0.11` — 0.10.0 widens `WatchStatusEmit.health` with `"unknown"` (cannobserv#328), the value the #264 status producer emits for a pre-first-check item; 0.9.3 carried `RegistryAnnouncementState` / `WatchStatusState` and the `INFO_REGISTRY` / `INFO_WATCH_STATUS` streams the registry consumer reads, cannobserv#324 and #254; 0.8.1 already carried the `spec_fingerprint` derivation the revisions producer imports unconditionally, cannobserv#309; `co-core-aio` carries the **`bus`** extra for the fetch-policy producer and the tail reader — #245). The 0.9 bump is **additive on every surface Watcher touches** — the `content.fetch` / `content.blobs` / `content.revisions` paths are untouched, and the `ChangeEventPayload` union simply widens; the MINOR is driven by `WordPressConfig` / `relationship.py` / `cdio` changes this repo has no exposure to. The 0.8 bump (cannobserv#300, adopted in #252) is **breaking in both directions**: `info_source_id` is required on all three content contracts and `BlobAvailableEvent.command_id` stopped being optional, so a 0.7.7 fact no longer decodes here — the deploy-ordering rule that follows is in [docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md) → "`info_source_id` on the wire". `co-core` carries the **`extract`** extra (`co-core[extract]`) — the heavy HTML/PDF/CSV parsers behind the extractors constructed in `src/core/registry.py`. The content-acquisition pipeline (fetch → extract → fingerprint) is now co-core's (`co_core.pure.extract.*`, `co_core.effects.fetch`, `co_core_aio.fetch`), adopted in #236; watcher no longer fetches at all (#241 step 5) — `src/core/fetch.py` is gone; the `watcher/0.1.0` User-Agent now lives in `src/core/fetch_commands.py` beside its only consumer and rides out on every `content.fetch` command's headers to preserve fingerprint byte-continuity. The systemd unit refreshes the wheelhouse via a non-fatal `ExecStartPre` so restarts self-heal (its output is plain text, not the app's JSON — see **Logging**).
