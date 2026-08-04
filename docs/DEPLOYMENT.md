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
| `WATCHER_CACHE_DIR` | env | no | Scratch directory for SourceRevision bytes (default `/var/cache/watcher/scratch`); must be writable by `watcher` user |
| `WATCHER_CACHE_TTL_SECONDS` | env | no | Scratch-file lifetime before sweeper removes it (default `600`) |
| `WATCHER_CACHE_SWEEP_INTERVAL_SECONDS` | env | no | Sweeper periodic interval in seconds (default `60`) |
| `BUILD_ID` | env | no | Git SHA for static asset cache-busting (default `"dev"`) |
| `NOTIFIER_BASE_URL` | `/etc/watcher/.env` | **yes** | Base URL of the notifier service (e.g. `http://localhost:9000`) |
| `NOTIFIER_API_KEY` | `/etc/watcher/.env` | **yes** | Watcher tenant API key issued by `scripts/seed_tenant.py` in the notifier repo |
| `ARCHIVER_BASE_URL` | `/etc/watcher/.env` | no | Archiver service base URL (default `http://localhost:8020`) |
| `ARCHIVER_API_KEY` | `/etc/watcher/.env` | **yes** | API key for the ArchiverClient SDK; missing key crashes the API on boot via the lifespan pre-warm |
| `WATCHER_ALLOW_PRODUCTION_DB` | `deploy/watcher.service` **only** | prod only | `=1` opts into serving a database whose name lacks a `_test`/`_dev` suffix (`src/core/db_safety.py`, #233). Must live in the systemd unit, never an env file — env files are sourced by hand-run dev servers, which are exactly what the guard stops |
| `WATCHER_DEV_DATABASE_URL` | `.env` | no | Persistent dev database for `scripts/dev_server.sh`; wins over `TEST_DATABASE_URL` |

**No Redis variable.** Archiver operates `redis-server` and owns the
`info.changes` change bus (archiver#109); Watcher neither produces to nor
consumes from it, and needs no Redis connection. See `AGENTS.md` § *Redis is
Archiver's* for the future paths that would reintroduce one — none is built.

## Systemd Service

A systemd unit file is provided at `deploy/watcher.service`.

### Installation

> **Install the Archiver service first** — see [§ Archiver Service](#archiver-service) below. `watcher.service` pre-warms an `ArchiverClient` in its lifespan and will crash-loop on missing `ARCHIVER_API_KEY` until the Archiver service section is complete.

```bash
# Create system env directory
sudo mkdir -p /etc/watcher
# Add production secrets (at minimum DATABASE_URL)
echo 'DATABASE_URL=postgresql+asyncpg://watcher:watcher@localhost:5432/watcher' | sudo tee /etc/watcher/.env
sudo chmod 640 /etc/watcher/.env
sudo chown root:exedev /etc/watcher/.env

# IMPORTANT: /etc/watcher/.env must exist before starting the service.
# Without it, systemd will refuse to start the unit (EnvironmentFile is required).

# Scratch directory for SourceRevision bytes
sudo mkdir -p /var/cache/watcher/scratch
sudo chown watcher:watcher /var/cache/watcher/scratch

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

# Follow WARNING+ only (records are JSON: timestamp/level/logger/message — #238).
# Needs jq (present on this VM at /bin/jq); the grep below is the no-jq fallback.
sudo journalctl -u watcher -f -o cat | jq 'select(.level == "WARNING" or .level == "ERROR" or .level == "CRITICAL")'
sudo journalctl -u watcher -f -o cat | grep -E '"level": "(WARNING|ERROR|CRITICAL)"'

# Reload after editing deploy/watcher.service
sudo systemctl daemon-reload && sudo systemctl restart watcher
```

Every line in the journal is JSON, uvicorn's included: the unit's `ExecStart`
passes `--log-config src/core/log_config.json`, which routes uvicorn's own
`uvicorn`/`uvicorn.access`/`uvicorn.error` loggers (`propagate=False`, plain-text
handlers by default) through the app's `build_json_formatter()` (#244), and
strips uvicorn's ANSI `color_message` duplicate from the payload (#246). Drop the
flag and the `jq` filter above silently skips the access/boot lines, which come
back as plain text. Same flag is baked into `scripts/dev_server.sh`.

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
checked out alongside this one — `/home/exedev/archiver` on this VM, and pinned
at `../archiver` by the `archiver-client` path dependency) that owns the
canonical InfoItem / InfoSource / SourceRevision / RepSpec registry. Watcher's lifespan pre-warms an `ArchiverClient` SDK against it;
without `ARCHIVER_API_KEY` and a reachable service, `watcher.service` will
refuse to boot.

See the Archiver repo's `docs/DEPLOYMENT.md` for the full Archiver install
(key generation, env-var registration, systemd unit). After installing
`archiver.service`, restart `watcher.service`:

```bash
sudo systemctl restart watcher
```

## Archiver Sync

SourceRevisions are POSTed to Archiver inline on change detection; anything that
fails lands in the local `pending_archiver_sync` outbox. The
`drain_pending_archiver_sync` periodic task
(`src/workers/source_revisions_drain.py`) retries that outbox on a fixed
**1-minute** cadence — a hardcoded Procrastinate `cron`, not an env var — so an
Archiver outage self-heals within a minute of the service returning.

The drain runs on the embedded worker inside the single uvicorn process; there
is no separate unit to start or monitor.

**Outbox / scratch interlock.** The cache sweeper skips any scratch file under
`WATCHER_CACHE_DIR` whose ULID still has a `pending_archiver_sync` row — the row
owns the file, and the drain drops the row only on a successful POST, after
which the file becomes a sweep candidate. This is a per-sweep query against the
outbox, not a lock: a row that never drains pins its scratch file indefinitely,
so a stuck outbox shows up as disk growth under `WATCHER_CACHE_DIR` rather than
as a failing check.

To spot one:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
# Backlog size, age of the oldest undrained row, and the last failure reason.
# psql needs a driverless URL — strip the SQLAlchemy "+asyncpg" dialect suffix.
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT count(*), min(created_at) AS oldest, max(attempts) AS max_attempts FROM pending_archiver_sync;"
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT id, attempts, last_error FROM pending_archiver_sync ORDER BY created_at LIMIT 5;"
# Scratch bytes currently held:
du -sh /var/cache/watcher/scratch
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

### Installation

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
