# Dashboard & Navigation Cleanup

**Date:** 2026-04-13

## Goal

Simplify the dashboard layout and sidebar by promoting System Health to a more prominent position and retiring the redundant `/system` route.

## Approved Approach

### 1. Reorder dashboard sections

Swap the two `mt-8` sections in `pages/dashboard.html` so **System Health** appears above **Recent Changes**. No logic changes — both sections already poll their respective partials independently.

### 2. Remove `/system` route

The dedicated `/system` page duplicates what the dashboard already shows via the `/partials/system-health` partial. Remove:
- `system_page` route handler (`routes.py`)
- `TestSystemPage` test class (`tests/dashboard/test_routes.py`)
- `pages/system.html` template

The `/partials/system-health` endpoint is retained — it is polled by the dashboard tiles.

### 3. Remove "System" from sidebar

Remove the `<a href="/system">` link from both the desktop nav and mobile drawer in `base.html`.

## Key Decisions

- **Keep `/partials/system-health`** — dashboard tiles depend on it; only the full page route is removed.
- **Delete `pages/system.html`** — dead file once the route is gone; no value in keeping it.
- **No redirect** — `/system` was internal-only navigation; no external links to preserve.

## Out of Scope

- Redesigning the System Health partial layout
- Adding any new system monitoring views
