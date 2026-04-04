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
src/core/models/       — SQLAlchemy models (Watch [+health_status: WatchHealthStatus], AuditLog, Snapshot, SnapshotChunk, Change [+visual_change_score float nullable], TemporalProfile, NotificationConfig [apprise_url encrypted, channel_hint, events ARRAY], Domain); notification_event_types seed table in migrations
src/core/probe.py      — URL probe: follow redirects, resolve effective URL and domain (ProbeResult + probe_url)
src/core/notifications/  — Apprise-based notification dispatcher; WatchEvent + WatchEventType + EVENT_TITLES (events.py), dispatch_event() (dispatcher.py)
src/core/extractors/   — Content extractors (HTML, PDF, CSV/Excel → Chunks)
src/core/fetchers/     — URL fetchers (HTTP; browser/WebRecorder planned)
src/core/differ.py     — Chunk-level change detection with SimHash similarity
src/core/simhash.py    — 64-bit SimHash fingerprinting
src/core/screenshot.py — Playwright screenshot capture (optional [browser] extra); ScreenshotResult + capture_screenshot; guards on PLAYWRIGHT_AVAILABLE
src/core/storage.py    — StorageBackend protocol + LocalStorage (save, load, exists, size, snapshot_path)
src/core/scheduler.py  — Watch scheduling logic (interval parsing, due computation, temporal profile resolution)
src/core/rate_limiter.py — Per-domain async rate limiting
src/core/config_poller.py  — Background polling: sync domain configs from DB into rate limiter
src/dashboard/           — Server-rendered dashboard (Jinja2 + HTMX + Tailwind); __init__.py registers Jinja2 globals: build_id, event_titles (human-readable event name map from EVENT_TITLES)
src/dashboard/routes.py  — Dashboard page and partial routes; includes POST /watches/{id}/screenshot (on-demand re-capture) and GET /watches/{id}/snapshots/{snapshot_id}/content (escaped text viewer)
src/dashboard/context.py — Dashboard-specific DB query helpers; includes get_latest_snapshot, compute_watch_health (pure fn), get_watch_health_map (per-watch latest check event), get_watch_timeline + get_watch_timeline_count (unified lifecycle timeline)
src/dashboard/static/    — CSS, JS (vendored HTMX, dark-mode, htmx-a11y), compiled Tailwind
src/dashboard/static/images/ — Brand assets and project icons (Cannabis Observer logo, magnifying glass)
src/dashboard/templates/ — Jinja2 templates (base, pages, partials); partials/pagination.html reusable offset-based pagination; partials/domain_field.html reusable inline-editable domain field (view/edit modes via GET /domains/{name}/field/{field_name}?mode=view|edit); partials/watch_field.html inline-editable watch field (text/number/textarea/select/toggle types, content-type-aware via WATCH_FIELD_META); partials/watch_status_toggle.html Active/Inactive/Archived toggle with badge; partials/watch_timeline.html unified lifecycle event timeline with category filter (change/error/run/config) and pagination; partials/watch_notifications.html interactive notification config list + add form (toggle/delete/test actions via HTMX to dashboard wrapper routes); macros/fields.html — watch_field(ctx) and domain_field(ctx) macros (import with context; centralise {% set %} boilerplate for field partials)
src/workers/           — Procrastinate task queue (check_watch, schedule_tick)
src/workers/pipeline.py  — Core check pipeline: hash, extract, diff, store snapshots
src/workers/notify.py    — Notification dispatch: dispatch_event_notifications(session, event)
src/core/crypto.py       — Fernet encryption for Apprise URLs (encrypt_apprise_url, decrypt_apprise_url); requires APPRISE_SECRET_KEY env var
src/core/registry.py     — ServiceRegistry: swappable fetcher and extractor implementations
tests/                 — Mirrors src/ structure
deploy/                — Systemd unit and deployment config
docs/                  — Reference docs (COMMANDS, SKILLS, DEPLOYMENT)
scripts/                 — Build scripts (Tailwind CSS)
```

**Environment files** (not in the repo tree):
- `/etc/watcher/.env` — Production secrets (`DATABASE_URL`); outside repo, persistent
- `.env` (repo root) — Dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`); git-ignored

## Infrastructure

**Single-VM setup.** This VM is both development and production. Code committed to main is the deployed code. The systemd service (`watcher`) runs the live site on port 8000.

| Service | Framework | Port | Managed by |
|---|---|---|---|
| API (live) | FastAPI | 8000 | `systemctl` (`watcher.service`) |
| API (dev) | FastAPI | 8001 | manual uvicorn |

The exe.dev proxy transparently forwards ports 3000–9999. Dev server on 8001 is accessible at `https://watcher.exe.xyz:8001/`.

## Server Lifecycle

**Port 8000 belongs to systemd.** Never start uvicorn manually on port 8000.

| Situation | Action |
|---|---|
| Code committed to main | `sudo systemctl restart watcher` |
| Testing a worktree/branch | `uv run uvicorn ... --port 8001 --reload` |
| Debugging the live service | `sudo journalctl -u watcher -f` |
| After editing `deploy/watcher.service` | `sudo systemctl daemon-reload && sudo systemctl restart watcher` |
| After Tailwind CSS changes | `bash scripts/build-css.sh` then restart |
| After DB model changes | `uv run alembic upgrade head` then restart |

**Dev server workflow:** Run on port 8001 so the live service stays up. Load env first:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

**After finishing work:** Always restart the systemd service to pick up changes merged to main:

```bash
sudo systemctl restart watcher
```

## Environment Variables

Two env files, loaded in order (later values override):

1. **`/etc/watcher/.env`** — production secrets (`DATABASE_URL`). Survives repo resets and worktree switches. Managed manually on the VM.
2. **`.env`** (repo root, git-ignored) — dev/agent secrets (`GH_TOKEN`, `TEST_DATABASE_URL`). Never commit.

The systemd service loads both automatically. For shell commands:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
```

Currently defined:
- `DATABASE_URL` — PostgreSQL connection string (in `/etc/watcher/.env`)
- `PROCRASTINATE_DATABASE_URL` — (optional) libpq-style DSN for procrastinate; falls back to DATABASE_URL with driver prefix stripped
- `GH_TOKEN` — GitHub personal access token (in `.env`)
- `TEST_DATABASE_URL` — PostgreSQL connection string for test database (in `.env`)
- `WATCHER_DATA_DIR` — (optional) absolute path for snapshot/content storage; defaults to `/var/lib/watcher/data`
- `BUILD_ID` — (optional) git SHA for static asset cache-busting; defaults to `"dev"`
- `APPRISE_SECRET_KEY` — Fernet key for encrypting Apprise URLs at rest (in `/etc/watcher/.env`); generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

## Common Commands

```bash
# Install dependencies
uv sync

# Load environment (required before running server, migrations, or gh)
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)

# Run tests
uv run pytest

# Run integration tests (requires PostgreSQL)
uv run pytest -m integration

# Run linter
uv run ruff check .

# Database migrations
uv run alembic upgrade head          # apply all migrations
uv run alembic revision --autogenerate -m "description"  # generate new migration

# FastAPI dev server (port 8001 — port 8000 belongs to systemd)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
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

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.segment-group`, `.segment`, `.chip-group`, `.chip`, `.detail-grid`, `.toggle`, `.danger-zone`). Badge variants: `.badge-active` (green), `.badge-inactive` (gray), `.badge-archived` (amber), `.badge-error` (red), `.badge-warning` (orange), `.badge-info` (blue). CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. Detect HTMX via `HX-Request` header with `HX-Boosted` guard. All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.
