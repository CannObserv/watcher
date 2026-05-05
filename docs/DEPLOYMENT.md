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
| `APPRISE_SECRET_KEY` | `/etc/watcher/.env` | **yes** | Fernet key for encrypting Apprise URLs at rest (raises at startup if missing) |
| `PROCRASTINATE_DATABASE_URL` | `/etc/watcher/.env` | no | libpq-style DSN for procrastinate; falls back to `DATABASE_URL` with driver prefix stripped |
| `GH_TOKEN` | `.env` | no | GitHub personal access token |
| `TEST_DATABASE_URL` | `.env` | no | PostgreSQL connection string for test database |
| `WATCHER_DATA_DIR` | env | no | Absolute path for snapshot/content storage (default `/var/lib/watcher/data`) |
| `BUILD_ID` | env | no | Git SHA for static asset cache-busting (default `"dev"`) |
| `NOTIFIER_BASE_URL` | `/etc/watcher/.env` | Phase 4+ | Base URL of the notifier service (e.g. `http://localhost:9000`) |
| `NOTIFIER_API_KEY` | `/etc/watcher/.env` | Phase 4+ | Watcher tenant API key issued by `scripts/seed_tenant.py` in the notifier repo |
| `USE_REMOTE_NOTIFY` | `/etc/watcher/.env` | Phase 4+ | Set to `"1"` to route notifications through notifier; default `"0"` (local Apprise) |
| `INFORMATION_BASE_URL` | `/etc/watcher/.env` | no | Information service base URL (default `http://localhost:8020`) |
| `INFORMATION_API_KEY` | `/etc/watcher/.env` | **yes** | API key for the InformationClient SDK; missing key crashes the API on boot via the lifespan pre-warm |
| `REDIS_URL` | `/etc/watcher/.env` | no | Redis connection URL for the change bus (default `redis://localhost:6379/0`) |

Generate `APPRISE_SECRET_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Systemd Service

A systemd unit file is provided at `deploy/watcher.service`.

### Installation

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

# Reload after editing deploy/watcher.service
sudo systemctl daemon-reload && sudo systemctl restart watcher
```

## Information Service

The Information service is a sibling FastAPI app (`src/information/`) that owns the canonical InfoItem + InfoSpec registry. Watcher's lifespan pre-warms an `InformationClient` SDK against it; without `INFORMATION_API_KEY` and a reachable service, `watcher.service` will refuse to boot.

A systemd unit file is provided at `deploy/information.service`.

### Installation

```bash
# Generate a strong API key (used by both the service and consumers)
KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# Add to /etc/watcher/.env (shared by both services — both load EnvironmentFile=/etc/watcher/.env)
echo "INFORMATION_BASE_URL=http://localhost:8020" | sudo tee -a /etc/watcher/.env
echo "INFORMATION_API_KEY=$KEY"                   | sudo tee -a /etc/watcher/.env
sudo chmod 640 /etc/watcher/.env  # group read required so the service user can load it

# Apply Information service migrations (separate alembic root)
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic -c alembic_information.ini upgrade head

# Install + enable + start
sudo cp deploy/information.service /etc/systemd/system/information.service
sudo systemctl daemon-reload
sudo systemctl enable --now information.service

# Verify
curl -s -H "X-API-Key: $INFORMATION_API_KEY" http://localhost:8020/api/v1/info-items
```

### Managing the Service

```bash
# Restart after code changes
sudo systemctl restart information

# Check status
sudo systemctl status information

# Follow logs
sudo journalctl -u information -f
```

After installing `information.service`, restart `watcher.service` so its lifespan pre-warm picks up the now-reachable SDK target:

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
