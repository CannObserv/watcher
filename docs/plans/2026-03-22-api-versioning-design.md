# API Versioning Prefix Design

**Issue:** #29
**Date:** 2026-03-22

## Goal

Add a `/v1` version segment to all API routes so paths become `/api/v1/<resource>`. Enables future breaking changes without disrupting clients.

## Approved Approach

Centralize the version prefix in `main.py`; strip `/api/` from each individual router prefix.

### Changes

1. **Each router** (`watches`, `changes`, `audit_log`, `temporal_profiles`, `notification_configs`, `domains`, `probe`) — change prefix from `/api/<resource>` → `/<resource>`.

2. **`main.py`** — create a v1 `APIRouter(prefix="/api/v1")`, include all 7 resource routers into it, then `app.include_router(v1_router)`.

3. **Tests** — update all URL strings from `/api/<resource>` → `/api/v1/<resource>`.

4. **Dashboard** — no changes needed (does not call API routes directly).

### Unversioned paths

`/api/watches`, `/api/probe`, etc. → **404**. No redirect. No backwards-compat shim.

## Out of Scope

- `/api/v2`, multi-version routing
- Deprecation headers
- Backwards-compat redirects
