# Common Commands

## Setup

```bash
# Install dependencies (creates .venv automatically)
uv sync
```

## Development

```bash
# FastAPI dev server (auto-reload)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
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

# Apply migrations (requires DATABASE_URL in env)
export $(cat env | xargs)
uv run alembic upgrade head

# Generate a new migration after model changes
uv run alembic revision --autogenerate -m "description of change"

# Check current migration state
uv run alembic current
```

## Task Queue (Procrastinate)

```bash
# Apply procrastinate schema (first time, after DB setup)
export $(cat env | xargs)
uv run procrastinate --app=src.workers.app schema --apply

# Run worker standalone (alternative to embedded mode in FastAPI)
uv run procrastinate --app=src.workers.app worker

# The worker also runs embedded in FastAPI via lifespan — no separate process needed for dev
```

## Tailwind CSS

```bash
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
git submodule update --remote --merge vendor/gregoryfoster-skills vendor/obra-superpowers
```
