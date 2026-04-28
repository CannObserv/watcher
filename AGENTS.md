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
src/api/routes/        — API endpoints (watches, temporal_profiles, changes, audit_log, notification_configs, notification_templates, domains, probe, apprise_plugins); mounted at /api/v1/
src/api/routes/notification_templates.py — CRUD + assign/unassign/test for shared templates; prefix /notifications/templates
src/api/routes/apprise_plugins.py — GET /api/v1/apprise/plugins (sorted list) and /api/v1/apprise/plugins/{schema} (token defs + variants); 404 for unknown schema
src/api/schemas/apprise_plugin.py — PluginListItem [+setup_url, service_url], PluginDetail [+setup_url, service_url], TokenMeta, PluginVariant response schemas
src/api/routes/health.py — /health (liveness) and /ready (readiness) endpoints; root-level, not versioned
src/core/              — Shared domain logic
src/core/models/       — SQLAlchemy models (Watch [+health_status: WatchHealthStatus, last_changed_at datetime nullable, tags ARRAY(String) nullable, description Text nullable], AuditLog, Snapshot, SnapshotChunk, Change [+visual_change_score float nullable], TemporalProfile, WatchNotificationConfig [apprise_url encrypted, channel_hint, title (optional, max 100), events ARRAY; table: watch_notification_configs], NotificationTemplate [reusable shared NC; title required, apprise_url encrypted, channel_hint, events, is_global_default, is_active], WatchNcRef [junction: template → watch], DomainNcRef [junction: template → domain default], Domain, AppUser [exe.dev identity anchor; lazy-upserted from X-ExeDev-UserID/X-ExeDev-Email headers on each dashboard login], ApiKey [hashed API keys; key_hash=SHA-256, key_prefix=first 8 chars of raw key, raw key never persisted]); notification_event_types seed table in migrations
src/core/probe.py      — URL probe: follow redirects, resolve effective URL and domain (ProbeResult + probe_url)
src/core/watches.py    — create_watch() service: probe → domain upsert → Watch insert → flush → audit → notify → commit; used by both API and dashboard routes
src/core/notifications/  — Apprise-based notification dispatcher; WatchEvent + WatchEventType + EVENT_TITLES (events.py; WatchEvent has no .title/.body — rendered from templates by the dispatcher), dispatch_event(*, body, title) → DispatchResult (dispatcher.py; body + title required, no fallback; body_format=NotifyFormat.MARKDOWN so Apprise downconverts the single canonical body for HTML/plaintext channels — see #116); _ASSET (AppriseAsset) applies CO Watcher branding to all outbound notifications; captures Apprise WARNING logs into DispatchResult.reason on failure (ContextVar-isolated per asyncio task); notify.py: dispatch_event_notifications() lazy-loads the unified diff once per change_detected event when at least one candidate consumes it (toggle on or body_template references diff_snippet/diff_full) via _load_event_unified_diff (Change → Snapshot → Watch.content_type: HTML watches diff prettified `storage_path` via normalize_html, mirroring the dashboard Raw-mode path #118; non-HTML watches diff `text_path` extracted text; normalize_html failure falls back to un-prettified raw HTML; accepts content_type kwarg threaded from the dispatcher's widened `select(Watch.effective_domain, Watch.content_type)` to skip the redundant Watch fetch); content.py: resolve_options() + build_title(*, strict=False) + build_body(*, strict=False, unified_diff=None; for change_detected, composes the body in Python by interleaving toggle-gated sections at the issue #104 positions — DOMAIN between watch_name and URL; CHANGE after WATCH; diff after change_summary; INTERVAL/LAST CHANGED/SIGNIFICANCE grouped; DESCRIPTION and TAGS last — the WATCH dashboard link is part of the unconditional skeleton, no toggle. Custom body_template bypasses toggles entirely and replaces the default) + render_template() (Jinja2, falls back to raw string on error) + render_template_strict() (StrictUndefined — raises on syntax + undefined; for preview) + build_template_context(*, unified_diff=None) (derived fields: event_label, occurred_at_iso, change_summary, change_url, diff_snippet, diff_full, chunks_changed); diff_snippet / diff_full render the precomputed unified diff in a Markdown ```diff fenced block, snippet capped via diff_snippet_lines with hunk-boundary truncation; chunks_changed is the structured list[{status, label, similarity}] replacement for the legacy chunk-label summary (#116); default_templates.py: DEFAULT_TITLE_TEMPLATES (all events prefixed `[Observo] ` for cross-service filtering) + DEFAULT_BODY_TEMPLATES (Jinja strings keyed by event_type value — most are dispatched directly; change_detected is the exception, composed in Python by `content.build_body` from the shared `CHANGE_DETECTED_HEADER_LINES` / `CHANGE_DETECTED_BODY_BLOCK_LINES` tuples — single source of truth for the always-present skeleton — with toggle-driven sections interleaved at the issue #104 positions; `DEFAULT_BODY_TEMPLATES['change_detected']` is derived from the same tuples and serves only as the UI seed); preview_fixtures.py: MOCK_EVENT_FIXTURES (canned per-event metadata; change_detected fixture carries previous_text/current_text) + build_preview_event() (stateless WatchEvent for preview endpoint) + compute_preview_unified_diff() (computes a real unified diff from the canned text for the live preview)
src/core/notifications/constants.py — APP_URL shared constant ("https://watcher.exe.xyz"); imported by dispatcher.py and content.py to avoid circular imports
src/api/schemas/content_config.py — ContentOptions (per-notification toggles: diff snippet/full, temporal context, domain, last_changed_at, significance, change_dashboard_url, tags, description; plus title_template/body_template Jinja2 strings — no include_watch_url toggle since WATCH is always in the default body); ContentConfig (default: ContentOptions + overrides: dict[event_type, ContentOptions]); resolve_options() lives in content.py
src/core/notifications/apprise_builder.py — Apprise plugin catalog introspection + URL assembly; list_plugins(), get_plugin_detail(schema), get_service_name(schema), assemble_url(schema, tokens, variant_index); returns setup_url + service_url per plugin; _build_catalog() + _list_plugins_cached() lru_cached
src/core/extractors/   — Content extractors (HTML, PDF, CSV/Excel → Chunks)
src/core/fetchers/     — URL fetchers (HTTP; browser/WebRecorder planned)
src/core/differ.py     — Chunk-level change detection with SimHash similarity
src/core/diff/         — Dashboard/notification diff service: normalize → compute → render (models.py: frozen+slots DiffResult [unified_diff, has_changes, added, removed]; normalize.py: normalize_text [CRLF/CR→LF + per-line rstrip], normalize_html [#118: html5lib parse + lxml.html.tostring(pretty_print=True), strips comments, preserves whitespace inside `<pre>`/`<textarea>`/`<script>`/`<style>`, idempotent via two-pass fixed-point (fast-path skips pass 2 when input is already a fixed point)]; textual.py: compute_unified_diff(prev, curr, *, context=3) via stdlib difflib; uses identical fromfile/tofile label "content" so diff2html doesn't emit a RENAMED badge)
src/core/simhash.py    — 64-bit SimHash fingerprinting
src/core/screenshot.py — Playwright screenshot capture (optional [browser] extra); ScreenshotResult + capture_screenshot; guards on PLAYWRIGHT_AVAILABLE
src/core/storage.py    — StorageBackend protocol + LocalStorage (save, load, exists, size, snapshot_path)
src/core/scheduler.py  — Watch scheduling logic (interval parsing, due computation, temporal profile resolution)
src/core/rate_limiter.py — Per-domain async rate limiting
src/core/config_poller.py  — Background polling: sync domain configs from DB into rate limiter
src/dashboard/           — Server-rendered dashboard (Jinja2 + HTMX + Tailwind); __init__.py registers Jinja2 globals: build_id, event_titles (human-readable event name map from EVENT_TITLES)
src/api/deps.py          — `require_api_key` FastAPI dependency; validates X-API-Key header (403 absent, 401 invalid), updates last_used_at, returns user_id; guards entire /api/v1/ router
src/dashboard/deps.py    — `get_dashboard_user` FastAPI dependency: validates X-ExeDev-UserID/X-ExeDev-Email headers, upserts AppUser, raises 307 redirect to /__exe.dev/login if headers absent; `generate_api_key()` utility: returns (raw_key, key_hash, key_prefix)
src/dashboard/settings.py — Settings routes (prefix /settings): GET/POST /settings (landing + API key count), GET/POST /settings/api-keys (list + create), GET /settings/api-keys/new-row (HTMX add-row form), GET/POST /settings/api-keys/{key_id}/edit-row, GET /settings/api-keys/{key_id}/read-row, DELETE /settings/api-keys/{key_id}
src/dashboard/routes.py  — Dashboard page and partial routes; includes POST /watches/{id}/deactivate (HTMX outerHTML row update; 303 redirect fallback); POST /watches/{id}/screenshot (on-demand re-capture), GET /watches/{id}/snapshots/{snapshot_id}/content (escaped text viewer), GET /partials/apprise-plugin-form (HTMX token form for selected Apprise plugin or raw URL fallback); Notification full-page routes (all PRG: POST success → 303, error → 200 re-render): GET/POST /notifications/new (template library new-record page), POST /notifications/{id}/edit (template library edit; no GET — edit-form replaced by page), POST /notifications/{id}/toggle, DELETE /notifications/{id}/delete, POST /notifications/{id}/test-result, GET/POST /watches/{id}/notifications/new (watch local-config new-record page), GET/POST /watches/{id}/notifications/{nc_id}/edit (watch local-config edit page; decrypts + pre-fills URL), GET/POST /domains/{name}/notifications/new (domain default new-record page); Notification HTMX partials: POST /notifications/preview (stateless live preview: parses full notification form + preview_event selector, renders title + body through strict Jinja, returns partials/notification_preview.html fragment with either preview or error card), GET /notifications/compose-title-prefill + GET /notifications/compose-body-prefill (HTMX: returns composed Jinja string for pre-filling title/body textareas from current form state), GET /notifications/overrides/add-picker (HTMX: returns the per-event override picker partial listing subscribed-but-not-overridden events), GET /notifications/overrides/card (HTMX: returns a new override card seeded from current default state; 400 on invalid event_type); Template assignment: GET /watches/{id}/notifications/assign-row, POST /watches/{id}/notifications/assign/{template_id}, POST /watches/{id}/notifications/unassign/{template_id}, POST /watches/{id}/notifications/copy-template/{template_id}, POST /watches/{id}/notifications/{nc_id}/copy (duplicate local config); Domain defaults: GET /domains/{name}/nc-defaults, POST /domains/{name}/nc-defaults/add/{template_id}, POST /domains/{name}/nc-defaults/remove/{template_id}, GET /domains/{name}/nc-defaults/assign-row (picker; excludes globals); GET /partials/watch-table (HTMX: watch list with search/status/domain/sort/order); GET /partials/domain-watches/{name} (HTMX: domain watch table with search/status/sort/order)
src/dashboard/context.py — Dashboard-specific DB query helpers; includes get_latest_snapshot, compute_watch_health (pure fn), get_watch_health_map (per-watch latest check event), get_watch_timeline + get_watch_timeline_count (unified lifecycle timeline); get_watch_list (search, domain, sort, order params); get_domain_watches (search, is_active, sort, order params)
src/dashboard/static/    — CSS, JS (vendored HTMX, dark-mode [emits `watcher:theme-changed` CustomEvent on toggle], htmx-a11y), compiled Tailwind; js/vendor/diff2html-ui.min.js (~1MB / ~300KB gzipped — includes diff2html core + highlight.js; page-scoped to change_detail, not loaded globally) + css/vendor/diff2html.layered.css (built by scripts/build-css.sh from diff2html.min.css; wrapped in @layer vendor — see docs/STYLE.md §14); js/diff-viewer.js initializes Diff2HtmlUI on `.diff-mount` elements (outputFormat from data-output-format, matching='words', colorScheme tracks `<html class="dark">`, re-renders on watcher:theme-changed + htmx:afterSwap when target is or contains #diff-content)
src/dashboard/static/images/ — Brand assets and project icons (Cannabis Observer logo, magnifying glass)
src/dashboard/templates/ — Jinja2 templates (base, pages, partials); partials/pagination.html reusable offset-based pagination; partials/domain_field.html reusable inline-editable domain field (view/edit modes via GET /domains/{name}/field/{field_name}?mode=view|edit); partials/watch_field.html inline-editable watch field (text/number/textarea/select/toggle types, content-type-aware via WATCH_FIELD_META); partials/watch_status_toggle.html Active/Inactive/Archived toggle with badge; partials/watch_timeline.html unified lifecycle event timeline with category filter (change/error/run/config) and pagination; partials/watch_notifications.html unified notification table with Source column (chip: Global/Domain/Assigned/Local); Global rows (purple tint, 🔒, read-only: Edit→library, Test); Domain rows (blue tint, 🏢, read-only: Edit→library, Test); Assigned rows (WatchNcRef: Edit→library, Test, Duplicate, Unassign); Local rows (full CRUD: Edit link → dedicated page, Test, Duplicate, Toggle, Delete); expects watch, notifications, global_templates, domain_templates, watch_templates, unassigned_templates; + Assign Template and + Add Local buttons link to dedicated new-record pages; partials/notification_preview.html live-preview fragment returned by POST /notifications/preview (expects either preview={title, body, event_label} or error={where, message}); partials/notification_form.html unified form body (Basics + Subscribe + Content + Per-event overrides + optional sticky Preview; used by watch_notification_edit.html and notification pages); partials/notification_form_content_card.html + notification_form_overrides_card.html + notification_form_preview_card.html individually-includable section partials used by new-record pages; partials/notification_form_content_body.html inner content (additive toggles + Default title + Default body blocks with [Edit template] / "Seed from toggles" controls), reused by overrides; partials/notification_form_override_card.html per-event override card (wraps content_body with a header + Remove button); partials/notification_form_override_picker.html inline picker inserted by GET /notifications/overrides/add-picker; partials/notification_variable_chips.html primary variable-chip row + [See all variables] reference drawer above Jinja textareas; partials/watch_nc_assign_row.html inline assign-from-library form (picker of unassigned templates); pages/notifications.html template library CRUD page; partials/notification_template_list.html tbody partial for HTMX refresh; partials/notification_template_row.html single template row; partials/domain_nc_defaults.html assigned/unassigned template picker for domain defaults; partials/apprise_plugin_form.html HTMX-swapped token form for a selected Apprise plugin (variant selector, required first, optional under "Advanced options"); partials/apprise_raw_url_form.html raw Apprise URL fallback input; pages/notification_new.html template library new-record full page; pages/watch_notification_new.html watch local-config new-record full page (title optional, watch_created disabled); pages/watch_notification_edit.html watch local-config edit full page (wraps notification_form.html; decryption-failed amber alert); pages/domain_notification_new.html domain default new-record full page (title required, all events enabled, form_id dots sanitized); pages/settings.html API key count landing; pages/settings_api_keys.html API keys table (#api-keys-tbody, #api-keys-modal-container); partials/api_key_row.html read-only key row (Edit outerHTML, Delete outerHTML with hx-confirm); partials/api_key_edit_row.html inline edit/new form row (new→POST to #api-keys-modal-container innerHTML, edit→POST outerHTML); partials/api_key_new_key_modal.html one-time raw-key display modal (Copy button, Done reloads page); macros/fields.html — watch_field(ctx) and domain_field(ctx) macros (import with context; centralise {% set %} boilerplate for field partials); macros/apprise_token_input.html — token_input(name, tok, required) macro for Apprise plugin token fields (shared by apprise_plugin_form.html); macros/watch_filters.html — watch_filters(base_url, target, q, status, show_domain, domain, sort, order) macro: renders search input, optional domain input, status segment-group, and hidden sort/order state inputs; partials/watch_table.html — main watch list with filters (via macro) and sortable headers (Name/Status/Health/Last Checked/Last Changed); expects watches, health_map, q, status, domain, sort, order; partials/domain_watches_table.html — domain-scoped watch table with filters (show_domain=false) and sortable headers (Name/Status/Health/Last Checked/Last Changed); expects domain, watches, health_map, q, status, sort, order
src/workers/           — Procrastinate task queue (check_watch, schedule_tick)
src/workers/pipeline.py  — Core check pipeline: hash, extract, diff, store snapshots
src/core/notifications/notify.py — Notification dispatch: dispatch_event_notifications(session, event) — queries 4 live sources in priority order: global templates (is_global_default=True), domain templates (DomainNcRef for watch.effective_domain), watch-assigned templates (WatchNcRef), local configs (WatchNotificationConfig); deduplicates by template_id; source field is "global"|"domain"|"watch_template"|"local"
src/workers/notify.py    — Backwards-compat re-export shim for dispatch_event_notifications; new code imports from src.core.notifications.notify directly
src/core/crypto.py       — Fernet encryption for Apprise URLs (encrypt_apprise_url, decrypt_apprise_url); requires APPRISE_SECRET_KEY env var
src/core/registry.py     — ServiceRegistry: swappable fetcher and extractor implementations
tests/                 — Mirrors src/ structure
deploy/                — Systemd unit and deployment config
docs/                  — Reference docs (COMMANDS, SKILLS, DEPLOYMENT)
scripts/                 — Build scripts: Tailwind compile (build-css.sh, check-css.sh) + vendor CSS layer-wrapping (wrap-vendor-css.py — see docs/STYLE.md §14)
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
| After Tailwind or vendor CSS changes | `bash scripts/build-css.sh` then restart |
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

**DB Triggers:**
- Triggers live in Alembic migrations (use `CREATE OR REPLACE FUNCTION` + `CREATE OR REPLACE TRIGGER`; downgrade with `DROP TRIGGER IF EXISTS … ON table; DROP FUNCTION IF EXISTS …`).
- Integration tests use `Base.metadata.create_all` (not migrations), so triggers are NOT applied automatically. Any trigger added in a migration must also be recreated in `tests/conftest.py` inside the `test_engine` session fixture, after the `create_all` call.
- Current triggers: `trg_changes_update_last_changed_at` (AFTER INSERT ON changes → sets `watches.last_changed_at = NEW.detected_at`).

## Style & UI Conventions

Authoritative reference: `docs/STYLE.md`

**Brand:** Cannabis Observer — `co-purple-600` (#6d4488) primary accent. Never use brand colors for semantic status (green/yellow/red/blue).

**Dark Mode:** Tailwind `dark:` variants on every color utility. Class-based toggle (`<html class="dark">`). localStorage key: `watcher-color-scheme`.

**Accessibility:** WCAG 2.1 AA. Skip link, ARIA landmarks, `focus-visible` rings, 44px touch targets, `aria-live` on HTMX swap targets, reduced motion. Wrap decorative emoji in `<span aria-hidden="true">`. No `title` attributes.

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.segment-group`, `.segment`, `.chip-group`, `.chip`, `.detail-grid`, `.toggle`, `.danger-zone`). Badge variants: `.badge-active` (green), `.badge-inactive` (gray), `.badge-archived` (amber), `.badge-error` (red), `.badge-warning` (orange), `.badge-info` (blue). CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. Detect HTMX via `HX-Request` header with `HX-Boosted` guard. All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.
