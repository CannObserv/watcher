# Deployment

Configuration — env files, every variable, and the unit-only credentials:
[ENVIRONMENT.md](ENVIRONMENT.md).

## Systemd Service

A systemd unit file is provided at `deploy/watcher.service`.

### Installation

> **Install the Archiver service first** — see [§ Archiver Service](#archiver-service) below. Watcher no longer holds an Archiver SDK and will boot without one (#254), but it consumes `info.registry` off the Archiver-operated broker, so until that is up `watched_items` cannot reconcile and no registry state arrives.

> **Join the tailnet first, too** (#280). Notifier runs on its own VM since
> notifier#43 and binds its **tailnet address alone** — its launch scripts
> resolve this host's `100.x` address and hand uvicorn that one `--host`, so it
> is unreachable from loopback, from exe.dev's internal `10.42.0.0/16`, and
> from the internet. So
> `notifier` in `WATCHER_NOTIFIER_BASE_URL` is a MagicDNS name on the
> `cannobserv.org.github` tailnet, and a host that has not joined cannot resolve
> or reach it at all.
>
> This failure is quieter than the old wrong-port one. The #277/#278 gates check
> that the URL and the flag are *held together*, not that the host answers, so a
> watcher off the tailnet **starts clean** and then fails every dispatch at call
> time. Join the tailnet, then confirm `curl http://notifier:9000/health`
> answers before starting the unit.

```bash
# Create system env directory
sudo mkdir -p /etc/watcher
# Add production secrets (at minimum DATABASE_URL)
echo 'DATABASE_URL=postgresql+asyncpg://watcher:watcher@localhost:5432/watcher' | sudo tee /etc/watcher/.env
sudo chmod 640 /etc/watcher/.env
sudo chown root:exedev /etc/watcher/.env

# The notifier credential goes in its OWN file, readable by root only (#278).
# It must never join /etc/watcher/.env: scripts/load-env.sh exports that file
# into every agent shell, and this is the pair that makes a stray dispatch
# deliverable to real subscribers.
sudo install -m 600 -o root -g root /dev/null /etc/watcher/notifier.env
sudo tee /etc/watcher/notifier.env >/dev/null <<'EOF'
WATCHER_NOTIFIER_BASE_URL=http://notifier:9000
WATCHER_NOTIFIER_API_KEY=<production tenant key from notifier's scripts/seed_tenant.py>
EOF

# IMPORTANT: /etc/watcher/.env and /etc/watcher/notifier.env must both exist
# before starting the service. Without either, systemd refuses to start the
# unit (both EnvironmentFile= lines are required).

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

Auth is ADC: on the VM/deploy the `co-pypi-reader` SA key at `GOOGLE_APPLICATION_CREDENTIALS` (in `/etc/watcher/.env`); in CI, keyless via Workload Identity Federation (`.github/workflows/ci.yml`). The identity needs only `roles/storage.objectViewer`. Reproducibility is `uv.lock` (pins the exact version), not wheelhouse contents. **Upgrade:** re-sync, then `uv lock --upgrade-package co-core` (bump the floor if the minor moved). Currently pinned: **v0.13.2** (floors `>=0.13.1,<0.14` — 0.13.1 adds `group_name`, the derived consumer-group naming the #285 rename adopts, and 0.13.2 changes only `co_v1` adapters; 0.10.0 widens `WatchStatusEmit.health` with `"unknown"` (cannobserv#328), the value the #264 status producer emits for a pre-first-check item; 0.9.3 carried `RegistryAnnouncementState` / `WatchStatusState` and the `INFO_REGISTRY` / `INFO_WATCH_STATUS` streams the registry consumer reads, cannobserv#324 and #254; 0.8.1 already carried the `spec_fingerprint` derivation the revisions producer imports unconditionally, cannobserv#309; `co-core-aio` carries the **`bus`** extra for the fetch-policy producer and the tail reader — #245). The 0.10 → 0.13 bump (#285) is **additive on every surface Watcher touches**, established by diffing the wheels rather than by reading release notes: across `co_core` / `co_core_aio` 0.10.0 → 0.13.2 the only module Watcher imports that changed at all is `pure/adapters/bus/streams.py`, and it changed additively (`StreamKind`, `stream_kind`, `group_name`; every stream constant byte-identical). `pure/models/changes.py`, `pure/adapters/bus/envelope.py`, `pure/adapters/bus/exceptions.py`, `pure/extract/*`, `pure/util/hashing.py` and **all of `co_core_aio/bus.py`** are byte-identical; everything else that moved is `pure/adapters/co_v1/*` and `pure/models/task_performer.py`, which this repo does not import. Re-run that diff on the next multi-minor bump — a floor spanning three MINORs is not covered by any one release's notes. The 0.9 bump is **additive on every surface Watcher touches** — the `content.fetch` / `content.blobs` / `content.revisions` paths are untouched, and the `ChangeEventPayload` union simply widens; the MINOR is driven by `WordPressConfig` / `relationship.py` / `cdio` changes this repo has no exposure to. The 0.8 bump (cannobserv#300, adopted in #252) is **breaking in both directions**: `info_source_id` is required on all three content contracts and `BlobAvailableEvent.command_id` stopped being optional, so a 0.7.7 fact no longer decodes here — the deploy-ordering rule that follows is in [docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md) → "`info_source_id` on the wire". `co-core` carries the **`extract`** extra (`co-core[extract]`) — the heavy HTML/PDF/CSV parsers behind the extractors constructed in `src/core/registry.py`. The content-acquisition pipeline (fetch → extract → fingerprint) is now co-core's (`co_core.pure.extract.*`, `co_core.effects.fetch`, `co_core_aio.fetch`), adopted in #236; watcher no longer fetches at all (#241 step 5) — `src/core/fetch.py` is gone; the `watcher/0.1.0` User-Agent now lives in `src/core/fetch_commands.py` beside its only consumer and rides out on every `content.fetch` command's headers to preserve fingerprint byte-continuity. The systemd unit refreshes the wheelhouse via a non-fatal `ExecStartPre` so restarts self-heal (its output is plain text, not the app's JSON — see **Logging**).
