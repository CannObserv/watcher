# Design: Retire compute_watch_health in favor of Watch.health_status

**Issue:** #72  
**Date:** 2026-04-13

## Goal

Remove the audit-log-derived health derivation (`compute_watch_health` / `get_watch_health_map`) and read `Watch.health_status` directly. Eliminates a redundant query and a divergence risk between two health representations.

## Approved Approach

Replace `_build_watch_health()` in `dashboard/routes.py` with a direct dict comprehension:

```python
health_map = {w.id: w.health_status for w in watches}
```

No helper function needed. The watch list query already loads `Watch` rows; `health_status` is available on each object.

## Key Decisions

**Accept loss of `"warning"` state.** The old logic flagged watches not checked within 2× their interval. `WatchHealthStatus` has no `WARNING` value. This is intentional — warning was a heuristic; the worker sets `OK`/`ERROR` authoritatively. Stale watches remain `UNKNOWN` or `ERROR`. A `WARNING` enum value can be added later if needed.

**Update template to match enum values.** `WatchHealthStatus.OK` is `"ok"`, not `"healthy"`. `watch_row.html` must check `health == "ok"` for the healthy badge. Drop the `"warning"` branch.

## Scope

**In scope:**
- Delete `compute_watch_health()` and `get_watch_health_map()` from `src/dashboard/context.py`
- Delete `_build_watch_health()` from `src/dashboard/routes.py`; inline the one-liner
- Update `partials/watch_row.html` to use `"ok"` instead of `"healthy"`; drop `"warning"` branch
- Delete corresponding tests in `tests/dashboard/test_context.py`
- Clean up now-unused imports

**Out of scope:**
- Adding `WatchHealthStatus.WARNING`
- Staleness detection
