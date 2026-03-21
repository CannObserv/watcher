# watcher — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Sacrifice grammar for density. Lead with the answer or action.

## Project Overview

Web service for monitoring cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff

## Project Layout

```
src/api/               — FastAPI app (ASGI, routes, schemas)
src/api/routes/        — API endpoints (watches, temporal_profiles, changes, audit_log)
src/core/              — Shared domain logic
src/core/models/       — SQLAlchemy models (Watch, AuditLog, Snapshot, SnapshotChunk, Change, TemporalProfile)
src/core/extractors/   — Content extractors (HTML, PDF, CSV/Excel → Chunks)
src/core/fetchers/     — URL fetchers (HTTP; browser/WebRecorder planned)
src/core/differ.py     — Chunk-level change detection with SimHash similarity
src/core/simhash.py    — 64-bit SimHash fingerprinting
src/core/storage.py    — StorageBackend protocol + LocalStorage
src/core/scheduler.py  — Watch scheduling logic (interval parsing, due computation, temporal profile resolution)
src/core/rate_limiter.py — Per-domain async rate limiting
src/workers/           — Procrastinate task queue (check_watch, schedule_tick)
tests/                 — Mirrors src/ structure
docs/                  — Reference docs (COMMANDS, SKILLS)
```

## Services

| Service | Framework | Port |
|---|---|---|
| API | FastAPI | 8000 |

```bash
# FastAPI dev server
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

After any code change in production deployments, restart uvicorn/gunicorn — they do not auto-reload.

## Secrets

`env` (git-ignored): API keys and tokens. Never commit secrets.

Load before running any command that needs env vars (e.g. `gh`):

```bash
export $(cat env | xargs)
```

Currently defined:
- `GH_TOKEN` — GitHub personal access token (used by `gh` CLI)
- `DATABASE_URL` — PostgreSQL connection string (used by SQLAlchemy and Alembic)
- `PROCRASTINATE_DATABASE_URL` — (optional) libpq-style DSN for procrastinate; falls back to DATABASE_URL with driver prefix stripped
- `WATCHER_DATA_DIR` — (optional) absolute path for snapshot/content storage; defaults to `/var/lib/watcher/data`

## Common Commands

```bash
# Install dependencies
uv sync

# Load environment (required before running server or migrations)
export $(cat env | xargs)

# Run tests
uv run pytest

# Run integration tests (requires PostgreSQL)
uv run pytest -m integration

# Run linter
uv run ruff check .

# Database migrations
uv run alembic upgrade head          # apply all migrations
uv run alembic revision --autogenerate -m "description"  # generate new migration

# FastAPI dev server
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Full reference: `docs/COMMANDS.md`

## Agent Skills

Skills in `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Reference: `docs/SKILLS.md`

## Conventions

**Commit Messages:**
```
#<number> [type]: <description>      # with issue
[type]: <description>                # without issue
```
Types: feat, fix, refactor, docs, test, chore

**Logging:**
```python
from src.core.logging import get_logger
logger = get_logger(__name__)
```
Entry points only: call `configure_logging()` once.

**Date & Time:**
- All UTC
- ISO 8601: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (timestamps), `YYYY-MM-DD` (dates)

**General:**
- No inline module imports; all at file top
- Docstrings for public modules, classes, functions
- Test structure mirrors source (`src/foo.py` → `tests/test_foo.py`)
- Explicit imports only
- Small, focused functions
