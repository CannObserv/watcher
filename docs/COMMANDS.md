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
# Dev server (port 8001) — the ONLY sanctioned launch path (#233).
# Targets TEST_DATABASE_URL (or WATCHER_DEV_DATABASE_URL), migrates it, and
# refuses any DB whose name lacks a _test/_dev suffix. Never hand-run uvicorn
# with the prod env loaded: /etc/watcher/.env points DATABASE_URL at
# production, and the embedded worker would consume the prod task queue.
bash scripts/dev_server.sh

# Knobs: WATCHER_DEV_PORT (default 8001; 8000 refused — it belongs to
# systemd), WATCHER_DEV_DATABASE_URL, WATCHER_DEV_SKIP_MIGRATE=1
```

Both sanctioned launch paths (`scripts/dev_server.sh` and the systemd
`ExecStart`) pass `--log-config src/core/log_config.json` so uvicorn's own
access/error lines are JSON like the app's (#244). Any ad-hoc uvicorn command
needs the same flag, or it emits plain text alongside the JSON records.

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
bash scripts/dev_server.sh   # same guard rails as the repo-root dev server
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

# Archiver service migrations live in the sibling Archiver repo.
# See its docs/COMMANDS.md.
```

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
