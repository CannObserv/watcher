# Design: DELETE /api/watches/{watch_id}

**Date:** 2026-03-22
**Issue:** #8

## Goal

Add a hard-delete endpoint for watches, enabling permanent removal for data cleanup, GDPR-style removal, or removing test watches.

## Approved Approach

### Endpoint

`DELETE /api/watches/{watch_id}` → 204 No Content

### Behavior

1. Fetch watch or return 404
2. If `is_active=True` → 409 Conflict ("deactivate watch before deleting")
3. Create `watch.deleted` audit log entry (payload: name, url) before deletion
4. Delete the watch row; database cascades handle all children
5. Return 204 No Content (empty body)

### Database Migration

Add FK cascade constraints:

| Table | FK Column | On Delete |
|---|---|---|
| snapshots | watch_id | CASCADE |
| changes | watch_id | CASCADE |
| temporal_profiles | watch_id | CASCADE |
| notification_configs | watch_id | CASCADE |
| audit_log | watch_id | SET NULL |
| snapshot_chunks | snapshot_id | CASCADE |
| changes | previous_snapshot_id | SET NULL |
| changes | current_snapshot_id | SET NULL |

### Audit Logging

- Event type: `watch.deleted`
- Payload: `{name, url}`
- Created before the delete; after cascade the `watch_id` FK becomes NULL, preserving the log entry

### Error Responses

- 404: Watch not found
- 409: Watch is still active (must deactivate first)

## Key Decisions

- **Database-level cascade** over application-level — safer, simpler, works outside the API
- **Require inactive** before delete — prevents accidental deletion of active watches
- **No `?force=true`** — deactivate-then-delete is sufficient; extra parameter adds complexity without value
- **Nullify audit log FK** — preserve audit trail after deletion
- **Nullify snapshot FKs on changes** — changes reference snapshots which cascade through watch; snapshot FKs on changes should SET NULL to avoid double-cascade issues

## Out of Scope

- Bulk delete
- Soft-delete/restore workflow beyond existing deactivate
- Retention policies or scheduled cleanup
