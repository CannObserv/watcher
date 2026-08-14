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
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
```

## Environment Variables

| Variable | Location | Required | Purpose |
|---|---|---|---|
| `DATABASE_URL` | `/etc/watcher/.env` | **yes** | PostgreSQL connection string |
| `PROCRASTINATE_DATABASE_URL` | `/etc/watcher/.env` | no | libpq-style DSN for procrastinate; falls back to `DATABASE_URL` with driver prefix stripped |
| `GH_TOKEN` | `.env` | no | GitHub personal access token |
| `TEST_DATABASE_URL` | `.env` | no | PostgreSQL connection string for test database |
| `BUILD_ID` | env | no | Git SHA for static asset cache-busting (default `"dev"`) |
| `NOTIFIER_BASE_URL` | `/etc/watcher/.env` | **yes** | Base URL of the notifier service (e.g. `http://localhost:9000`) |
| `NOTIFIER_API_KEY` | `/etc/watcher/.env` | **yes** | Watcher tenant API key issued by `scripts/seed_tenant.py` in the notifier repo |
| `WATCHER_ALLOW_PRODUCTION_DB` | `deploy/watcher.service` **only** | prod only | `=1` opts into serving a database whose name lacks a `_test`/`_dev` suffix (`src/core/db_safety.py`, #233). Must live in the systemd unit, never an env file — env files are sourced by hand-run dev servers, which are exactly what the guard stops |
| `WATCHER_DEV_DATABASE_URL` | `.env` | no | Persistent dev database for `scripts/dev_server.sh`; wins over `TEST_DATABASE_URL` |
| `WATCHER_BUS_REDIS_URL` | `/etc/watcher/.env` | prod | Redis URL of the Archiver-operated broker (`redis://localhost:6379/0`) for the `content.fetch-policy` and `info.watch-status` producers (#245, #264). Unset → both periodic publish tasks skip with an ERROR log: Replicator paces every host at its own conservative default, and Archiver's watched-item panel / drift detector go stale |
| `WATCHER_WATCH_STATUS_REPUBLISH_CRON` | env | no | Cron expression for the `info.watch-status` full-set republish (default `*/5 * * * *`, #264). The period is the recovery bound for a dropped frame — this stream has no outbox by design; loss is corrected by the next full set. A malformed value falls back to the default with an ERROR log |
| `WATCHER_WATCH_STATUS_STREAM_MAXLEN` | env | no | Producer-enforced retention cap for `info.watch-status` (`XADD MAXLEN ~`, default `50000`). The full set republishes forever, so an untrimmed stream grows without bound; invalid/non-positive values fall back to the default with a warning — never to unbounded |
| `WATCHER_FETCH_POLICY_STREAM_MAXLEN` | env | no | Same producer-enforced retention cap for `content.fetch-policy` (default `50000`) |
| `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS` | env | no | Reaper timeout for an in-flight command with no fact (default `1800` — deliberately generous; Replicator's reclaim cadence is an operator knob, and a tight value re-issues under live retries) |
| `WATCHER_FETCH_MAX_REISSUES` | env | no | Re-issues per fetch intent before it fails with ERROR health (default `3`) |
| `WATCHER_DEV_BUS_REDIS_URL` | `.env` | no | Scratch-bus opt-in for `scripts/dev_server.sh`; without it the dev server **clears** an inherited `WATCHER_BUS_REDIS_URL` so it cannot publish policy onto the production stream |

**Watcher's Redis use.** Archiver operates `redis-server` and owns the broker
(archiver#109). Watcher publishes `content.fetch-policy` (#245) and — Phase 4,
#241 — publishes `content.fetch` commands and consumes `content.blobs` facts
via its own consumer group (`watcher`, started in the lifespan when
`WATCHER_BUS_REDIS_URL` is set). Since #254 it also consumes `info.registry`
**grouplessly**, replayed from `0-0` at boot; without the bus URL neither
consumer starts and the registry cannot converge. Since #264 it publishes
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

Migrations are **not** run by the systemd unit or the app lifespan — they are a
manual step. After a deploy that changes DB models:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic upgrade head
sudo systemctl restart watcher
```

A fresh host bootstraps the full schema the same way (`alembic upgrade head`
against an empty database). The chain is self-contained — it references no
Archiver-owned schema — and is smoke-checked in CI (`migrations` job, #234).

### Restart-before-migrate — one-time, `d5a71c93e0f2` (#251)

`d5a71c93e0f2` makes `watched_items.archiver_info_item_id` and
`archiver_info_source_id` NOT NULL. The same release deletes the two code paths
that could produce a row without them (the dashboard create form and the API's
URL-only branch), so for **this one migration** the order above is reversed:

```bash
sudo systemctl restart watcher      # new code first — no path can write a bare row
uv run alembic upgrade head         # then the constraint
```

Migrating first leaves the old code briefly serving `/watched-items/new`, whose
insert then violates the new constraint and 500s. Production held zero bare rows
at the time of writing, so the migration itself is a metadata-only lock on four
rows; the ordering is about the window, not the data. Subsequent deploys use the
standard order above.

### No safe order — one-time, `e7c4b2a91f60` (#252)

`e7c4b2a91f60` adds `fetch_commands.info_source_id` NOT NULL, and the release
that needs it is the same release that populates it. Unlike `d5a71c93e0f2`
above, **neither order avoids a window**:

- Migrate first → the still-running old code's `create_fetch_command` omits the
  column, and every INSERT raises `NotNullViolation`.
- Restart first → the new code names a column the database does not have yet.

Run the two back-to-back and accept the seconds in between:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic upgrade head && sudo systemctl restart watcher
```

What fails in that window is bounded and self-healing, and it is only the two
paths that **INSERT** a command row: `check_watched_item` on each
`schedule_tick`, and the reaper's re-issues. Both are periodic — the next tick
after the restart succeeds, and no WatchedItem is left in a bad state. (The
pending-publish sweep and the fact consumer only UPDATE existing rows, so
neither is affected; a command already in flight rides the window out and its
row is backfilled.) In the journal it looks like

```
null value in column "info_source_id" of relation "fetch_commands"
```

on a handful of procrastinate jobs, then silence. That is the expected shape of
this deploy, not a symptom of something worse.

**This deploy also has a cross-service prerequisite:** Replicator must be
publishing `info_source_id` on its facts (CannObserv/replicator#28) *before*
watcher restarts onto co-core 0.8.0, or the fact consumer cannot decode them.
See [CONTENT-PIPELINE.md](CONTENT-PIPELINE.md) → "`info_source_id` on the wire".

### Migration baseline (squash) — one-time stamp

The pre-#234 migration chain was squashed into a single genesis baseline
(`2addddea0b03`, #234). This removed a transitional cross-schema FK into the
Archiver `information` schema that made `alembic upgrade head` fail from a clean
database.

Because the old version files were removed, an **already-migrated** database
(production, or any long-lived dev DB) has an `alembic_version` pointing at a
revision that no longer exists — a plain `upgrade head` there fails with
`Can't locate revision …`. **Once**, at the deploy that first lands the squash,
stamp the baseline instead of upgrading.

**First, confirm the database is exactly at the pre-squash HEAD** (`c5d6e7f8a9b0`,
the #218 audit-log-indexes migration). `stamp --purge` asserts the baseline
*regardless of where the DB actually is* — if the DB is behind `c5d6e7f8a9b0`,
stamping would silently mark the missing migrations as applied and corrupt the
schema. So this is a **halt-on-mismatch** gate, not a formality:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
# psql needs a driverless URL — strip the SQLAlchemy "+asyncpg" dialect suffix.
psql "${DATABASE_URL/+asyncpg/}" -c "SELECT version_num FROM alembic_version"
```

- If it prints **`c5d6e7f8a9b0`** → proceed to the stamp below.
- **Any other value → STOP.** The DB is not at the pre-squash HEAD; do not stamp.
  Reconcile it first (upgrade it to `c5d6e7f8a9b0` using the pre-squash version
  files from git history, or investigate why it diverged) before squashing.

> Production is expected to already be at `c5d6e7f8a9b0` — this gate should pass
> on the first read. A mismatch means something unusual happened to the DB; do
> not improvise the stamp, reconcile as above.

```bash
# Only after confirming the version is c5d6e7f8a9b0:
uv run alembic stamp 2addddea0b03 --purge   # re-point bookkeeping; schema unchanged
uv run alembic upgrade head                 # subsequent upgrades work normally
```

`stamp` only rewrites the `alembic_version` bookkeeping row; it makes no schema
changes. Fresh databases created after the squash need no stamp — they run the
genesis migration normally.

> The squash intentionally dropped two pieces of dead cruft from the *baseline*
> (an orphaned `trg_fn_watches_last_changed_at()` function and the vestigial
> `notification_event_types` catalog table). Existing databases still carry them
> harmlessly after the stamp; an optional cleanup migration can drop them later.

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
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
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
