# Common Commands

## Setup

```bash
# Install dependencies (creates .venv automatically)
uv sync
```

## Environment

Two env files, loaded in order:

```bash
# Production secrets (DATABASE_URL) — persistent, survives repo resets
/etc/watcher/.env

# Dev/agent secrets (GH_TOKEN, TEST_DATABASE_URL) — repo root, git-ignored
.env

# Load both for shell commands
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
```

The systemd service loads both automatically (see `deploy/watcher.service`).

## Service Management

The watcher service runs via systemd. **Always use systemctl** — never start uvicorn manually on port 8000.

```bash
# Restart after code changes (migrations are NOT auto-run)
sudo systemctl restart watcher

# Check status
sudo systemctl status watcher

# Follow logs
sudo journalctl -u watcher -f

# Reload systemd after editing deploy/watcher.service
sudo systemctl daemon-reload && sudo systemctl restart watcher
```

## Development

```bash
# Dev server — use a non-conflicting port so the systemd service stays up
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload

# CAUTION: Only stop the service if you need port 8000 specifically.
# The live site will be DOWN until you restart. Prefer port 8001.
# sudo systemctl stop watcher
# uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
# sudo systemctl start watcher  # MUST restart when done
```

### Testing code changes against the live site

After committing to main, restart the service to pick up changes:

```bash
sudo systemctl restart watcher
# Verify
curl -s http://localhost:8000/health | python3 -m json.tool
```

### Worktree testing

Run a worktree build on a different port to avoid conflicting with the service:

```bash
cd .worktrees/<branch>
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

## Testing

```bash
# Run all tests (excludes integration)
uv run pytest

# Run with coverage
uv run pytest --cov

# Run a specific file
uv run pytest tests/path/to/test_file.py --no-cov

# Run integration tests (hits live external services)
uv run pytest -m integration
```

## Linting

```bash
# Check
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .
```

## Database

```bash
# PostgreSQL setup (first time)
sudo apt-get install -y postgresql postgresql-client
sudo systemctl start postgresql
sudo -u postgres psql -c "CREATE USER watcher WITH PASSWORD 'watcher';"
sudo -u postgres psql -c "CREATE DATABASE watcher OWNER watcher;"
sudo -u postgres psql -c "CREATE DATABASE watcher_test OWNER watcher;"

# Watcher migrations (requires DATABASE_URL in env)
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description of change"
uv run alembic current

# Information service migrations (requires INFORMATION_DATABASE_URL in env)
uv run alembic -c alembic_information.ini upgrade head
uv run alembic -c alembic_information.ini revision --autogenerate -m "description of change"
```

## Change bus (Redis Streams)

Watcher publishes `info.changes` events to Redis Streams via `ChangePublisher`. The drain worker reads unpublished rows from the `changes` table outbox columns and forwards them to Redis.

Envelope shape is `schema_version: 2` (Phase 2c). Each entry is partitioned by `info_item_id` (was `watch_id` in Phase 2b's v1 shape) and carries `info_item_id`, `info_spec_id`, and `previous_fingerprint`/`current_fingerprint` so consumers can route or dedupe by Information Item without hitting Watcher.

The `drain_changes_outbox` task is registered via `@bp.periodic(cron="* * * * *")`, so the embedded Procrastinate worker fires it every minute. Manual invocation is still available for one-shot runs:

```bash
# Run the reference consumer (requires Redis running on REDIS_URL):
uv run python tools/info_changes_consumer.py --group archive-ref --output /tmp/info-changes.jsonl

# Inspect a stream's contents quickly:
redis-cli XLEN info.changes
redis-cli XRANGE info.changes - +

# Drain unpublished Changes manually (Procrastinate task):
uv run python -c "import asyncio; from src.workers.changes_drain import drain_changes_outbox; print(asyncio.run(drain_changes_outbox.func()))"
```

Note: `drain_changes_outbox.func()` calls the underlying async function directly, bypassing Procrastinate's queue dispatch — useful for manual one-shot runs. A PostgreSQL transaction-scoped advisory lock (`DRAIN_ADVISORY_LOCK_ID`) keeps the periodic drain and a manual run from double-publishing.

## Task Queue (Procrastinate)

```bash
# Apply procrastinate schema (first time, after DB setup)
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run procrastinate --app=src.workers.app schema --apply

# Run worker standalone (alternative to embedded mode in FastAPI)
uv run procrastinate --app=src.workers.app worker

# The worker also runs embedded in FastAPI via lifespan — no separate process needed for dev
```

## Tailwind CSS

```bash
# One-time VM setup (tailwindcss CLI — global npm)
sudo npm install -g @tailwindcss/cli

# Build Tailwind CSS
bash scripts/build-css.sh

# Watch mode (auto-rebuild on changes)
bash scripts/build-css.sh --watch
```

## Git Submodules

```bash
# Init after cloning
git submodule update --init --recursive

# Force-refresh vendor skills
git submodule update --remote --merge skills-vendor/gregoryfoster-skills skills-vendor/obra-superpowers
```
