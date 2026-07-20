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
| `REDIS_URL` | `/etc/watcher/.env` | no | Redis connection URL for the change bus (default `redis://localhost:6379/0`) |
| `CHANGES_DRAIN_INTERVAL_SECONDS` | env | no | Fast-tick changes-outbox drain cadence in seconds (default `10`); the 1-minute Procrastinate periodic stays as a safety floor |
| `WATCHER_ALLOW_PRODUCTION_DB` | `deploy/watcher.service` **only** | prod only | `=1` opts into serving a database whose name lacks a `_test`/`_dev` suffix (`src/core/db_safety.py`, #233). Must live in the systemd unit, never an env file — env files are sourced by hand-run dev servers, which are exactly what the guard stops |
| `WATCHER_DEV_DATABASE_URL` | `.env` | no | Persistent dev database for `scripts/dev_server.sh`; wins over `TEST_DATABASE_URL` |

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

# Reload after editing deploy/watcher.service
sudo systemctl daemon-reload && sudo systemctl restart watcher
```

## Archiver Service

The Archiver is a sibling service at `/home/exedev/archiver` (port 8020,
`archiver.service`) that owns the canonical InfoItem / InfoSource / SourceRevision / RepSpec
registry. Watcher's lifespan pre-warms an `ArchiverClient` SDK against it;
without `ARCHIVER_API_KEY` and a reachable service, `watcher.service` will
refuse to boot.

See `/home/exedev/archiver/docs/DEPLOYMENT.md` for the full Archiver install
(key generation, env-var registration, systemd unit). After installing
`archiver.service`, restart `watcher.service`:

```bash
sudo systemctl restart watcher
```

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
