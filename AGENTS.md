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
src/api/routes/        — API endpoints (watches, temporal_profiles, changes, audit_log, notification_configs, domains, probe); mounted at /api/v1/
src/api/routes/health.py — /health (liveness) and /ready (readiness) endpoints; root-level, not versioned
src/core/              — Shared domain logic
src/core/models/       — SQLAlchemy models (Watch, AuditLog, Snapshot, SnapshotChunk, Change, TemporalProfile, NotificationConfig, Domain)
src/core/probe.py      — URL probe: follow redirects, resolve effective URL and domain (ProbeResult + probe_url)
src/core/notifications/  — Notification channels (webhook, email, Slack) and dispatcher
src/core/extractors/   — Content extractors (HTML, PDF, CSV/Excel → Chunks)
src/core/fetchers/     — URL fetchers (HTTP; browser/WebRecorder planned)
src/core/differ.py     — Chunk-level change detection with SimHash similarity
src/core/simhash.py    — 64-bit SimHash fingerprinting
src/core/storage.py    — StorageBackend protocol + LocalStorage
src/core/scheduler.py  — Watch scheduling logic (interval parsing, due computation, temporal profile resolution)
src/core/rate_limiter.py — Per-domain async rate limiting
src/core/config_poller.py  — Background polling: sync domain configs from DB into rate limiter
src/dashboard/           — Server-rendered dashboard (Jinja2 + HTMX + Tailwind)
src/dashboard/routes.py  — Dashboard page and partial routes
src/dashboard/context.py — Dashboard-specific DB query helpers
src/dashboard/static/    — CSS, JS (vendored HTMX, dark-mode, htmx-a11y), compiled Tailwind
src/dashboard/static/images/ — Brand assets (Cannabis Observer icon)
src/dashboard/templates/ — Jinja2 templates (base, pages, partials)
src/workers/           — Procrastinate task queue (check_watch, schedule_tick)
src/workers/pipeline.py  — Core check pipeline: hash, extract, diff, store snapshots
src/workers/notify.py    — Notification dispatch: dispatch_change_notifications()
src/core/registry.py     — ServiceRegistry: swappable fetcher, extractor, channel implementations
tests/                 — Mirrors src/ structure
deploy/                — Systemd unit and deployment config
docs/                  — Reference docs (COMMANDS, SKILLS, DEPLOYMENT)
scripts/                 — Build scripts (Tailwind CSS)
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
- `TEST_DATABASE_URL` — PostgreSQL connection string for test database (used by pytest)
- `WATCHER_DATA_DIR` — (optional) absolute path for snapshot/content storage; defaults to `/var/lib/watcher/data`
- `BUILD_ID` — (optional) git SHA for static asset cache-busting; defaults to `"dev"`

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

## Style & UI Conventions

Authoritative reference: `docs/STYLE.md`

**Brand:** Cannabis Observer — `co-purple-600` (#6d4488) primary accent. Never use brand colors for semantic status (green/yellow/red/blue).

**Dark Mode:** Tailwind `dark:` variants on every color utility. Class-based toggle (`<html class="dark">`). localStorage key: `watcher-color-scheme`.

**Accessibility:** WCAG 2.1 AA. Skip link, ARIA landmarks, `focus-visible` rings, 44px touch targets, `aria-live` on HTMX swap targets, reduced motion. Wrap decorative emoji in `<span aria-hidden="true">`. No `title` attributes.

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.filter-pill`, `.detail-grid`). CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. Detect HTMX via `HX-Request` header with `HX-Boosted` guard. All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.
