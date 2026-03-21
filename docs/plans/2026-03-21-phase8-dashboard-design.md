# Phase 8: Dashboard — Design Doc

## Goal

Server-rendered dashboard for monitoring watch status, managing watches, viewing change history with diffs, and observing system health. API-first principle preserved — dashboard is a consumer of the same data layer.

## Approved Approach

Jinja2 templates + HTMX + Tailwind CSS. No Node.js. Externalized JS files. Direct DB access (not internal API calls). Pre-commit hook validates CSS compilation.

## Tech Stack

- **Templates:** Jinja2 via FastAPI `TemplateResponse`
- **Interactivity:** HTMX (vendored, no CDN)
- **Styling:** Tailwind CSS via standalone CLI binary
- **JS:** Externalized files (`htmx.min.js` vendored, `app.js` for custom behavior)
- **CSS build validation:** `pre-commit` library with custom hook
- **Form handling:** `python-multipart` for POST form data

## Pages

| Page | Route | Purpose |
|---|---|---|
| Dashboard | `/` | Stats cards, recent changes, system health |
| Watches | `/watches` | List with status indicators, filtering |
| Watch Detail | `/watches/{id}` | Full CRUD, profiles, notifications, change history |
| Watch Create | `/watches/new` | Create form |
| Watch Edit | `/watches/{id}/edit` | Edit form |
| Change Detail | `/changes/{id}` | Metadata, chunks, side-by-side diff |
| Audit Log | `/audit` | Filterable event stream |
| System | `/system` | Queue health, rate limiter state |

## File Structure

```
src/dashboard/
  __init__.py          — register_dashboard(app)
  routes.py            — all dashboard routes
  context.py           — shared query helpers (stats, watch status, queue health)
  static/
    css/
      input.css        — Tailwind source
      output.css       — compiled (gitignored)
    js/
      htmx.min.js      — vendored HTMX
      app.js           — custom JS (diff toggle, auto-refresh config)
  templates/
    base.html          — layout (sidebar nav, head, scripts)
    partials/          — HTMX-swappable fragments
      stats_cards.html
      recent_changes.html
      system_health.html
      watch_table.html
      change_list.html
    pages/
      dashboard.html
      watches.html
      watch_detail.html
      watch_form.html
      change_detail.html
      audit_log.html
      system.html
```

## HTMX Patterns

- **Partial loading:** `HX-Request` header detected in routes. HTMX requests get just the partial; full requests get layout + partial.
- **Auto-refresh:** Dashboard stats and system health poll via `hx-trigger="every Ns"`.
- **Inline actions:** Deactivate watch, delete profile/notification config via `hx-post`/`hx-delete` with row replacement.
- **Filtering:** Watch list and audit log filter via `hx-get` with query params, replacing table body.
- **Forms:** Standard POST, server-side validation, redirect on success.

## Data Access

Direct DB queries via shared `get_db_session` dependency. Dashboard-specific aggregations in `context.py`:

- **Stats:** Count watches, active watches, changes today, checks today
- **Watch status:** Join watches + latest snapshot for last-checked, next-due, last-result
- **Queue health:** Query `procrastinate_jobs` table for pending/running/failed/succeeded counts
- **Rate limiter:** Call `get_rate_limiter()` from tasks.py, expose domain states as dicts

## Diff Rendering

- Server-side diff via `difflib.unified_diff` on extracted text from storage
- Rendered as side-by-side HTML table
- Raw content toggle: HTMX fetches alternate partial with raw content diff
- Default: extracted text (matches what the differ compares)

## Pre-commit CSS Validation

Hook script checks if any `.html` template or `input.css` changed. If so, runs Tailwind CLI and compares output. Fails commit with rebuild instructions if output.css is stale.

## Implementation Phases

### Phase 8a — Foundation + Dashboard Home
- Tailwind CLI setup, pre-commit hook, static file mounting
- Base template, sidebar nav, layout
- `register_dashboard(app)`, HTMX partial pattern
- Dashboard home: stats cards, recent changes, system health
- Auto-refresh via HTMX polling

### Phase 8b — Watch Management
- Watch list (status indicators, filtering)
- Watch detail (config, profiles, notifications, change history)
- Watch create/edit forms with validation
- Deactivate via HTMX

### Phase 8c — Change Detail & Audit Log
- Change detail (metadata, chunks, side-by-side diff with raw toggle)
- Audit log (filterable event stream)
- System page (queue health detail, rate limiter state)

## Out of Scope

- Authentication/authorization (future)
- Real-time WebSocket updates (HTMX polling is sufficient at this scale)
- Phase 7 fetcher management UI (deferred with Phase 7)
- Mobile-optimized layout (desktop-first, responsive basics only)
