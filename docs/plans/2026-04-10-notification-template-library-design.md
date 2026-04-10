# Notification Template Library Design

**Date:** 2026-04-10
**Status:** Approved

## Goal

Allow users to define reusable Notification Configurations (NCs) once, then assign them by reference to any number of Watches and/or Domains. Edits to a shared template propagate automatically to all assignments. Rename the existing `notification_configs` table to `watch_notification_configs` to clarify its role within the expanded schema.

## Approved Approach: Separate Library Table + Junction Tables (Option A)

New `notification_templates` table is a first-class entity. Two junction tables (`watch_nc_refs`, `domain_nc_refs`) track assignments. Existing `watch_notification_configs` (renamed from `notification_configs`) handles watch-local NCs. Dispatch unions both sources.

## Key Decisions and Rationale

**True reference semantics.** Edits to a template propagate to all current assignments. Rationale: a shared library eliminates duplicative changes across hundreds of watches; lock friction prevents accidental edits; copy-to-local breaks the link for intentional local divergence.

**`notification_configs` renamed to `watch_notification_configs`.** Pre-production, no backward-compat concern. Makes the schema self-documenting alongside the new tables.

**Templates require a title.** Unlike watch-local NCs (title optional), templates must have a human-readable title — they appear in picker UIs and must be distinguishable at a glance.

**Auto-assignment on Watch create only.** Global defaults and domain defaults apply to newly created watches, not retroactively. Retroactive application would be an explicit bulk-assign user action if ever needed.

**Domain defaults are additive.** A watch created under a domain with NC defaults inherits both global defaults and domain defaults (union, deduplicated).

**Delete blocked if refs exist.** Deletion requires all `watch_nc_refs` and `domain_nc_refs` to be removed first. UI shows ref counts and offers "unassign from all then delete" convenience.

## Data Model

### New: `notification_templates`

| Column | Type | Notes |
|---|---|---|
| `id` | ULID PK | |
| `title` | String(100) NOT NULL | Required; used in picker UIs |
| `apprise_url` | Text NOT NULL | Fernet-encrypted |
| `channel_hint` | String(50) NOT NULL | URL scheme for display (e.g. "slack") |
| `events` | ARRAY(String(50)) NOT NULL | Default `["change_detected"]` |
| `is_global_default` | Boolean NOT NULL | Auto-assign to every new Watch |
| `is_active` | Boolean NOT NULL | Pause without deleting |
| `created_at`, `updated_at` | DateTime TZ | TimestampMixin |

### New: `watch_nc_refs`

| Column | Type | Notes |
|---|---|---|
| `watch_id` | ULID FK → watches | Composite PK |
| `template_id` | ULID FK → notification_templates | Composite PK |
| `created_at` | DateTime TZ | |

### New: `domain_nc_refs`

| Column | Type | Notes |
|---|---|---|
| `domain_name` | String FK → domains | Composite PK |
| `template_id` | ULID FK → notification_templates | Composite PK |
| `created_at` | DateTime TZ | |

### Renamed: `notification_configs` → `watch_notification_configs`

Schema otherwise unchanged (id, watch_id, title, apprise_url, channel_hint, events, is_active, created_at, updated_at).

## Navigation & Page Structure

### New top-level page: `/notifications`

Added to main nav. Owns template library CRUD.

- **Template list** — data table: Title, Channel, Events, Global Default badge, Active badge, Actions (Edit, Test, Toggle active, Delete).
- **Add template** — same builder UX (plugin picker → token form or raw URL) as watch-local add.
- **Edit template** — full edit form; no lock friction here (this is the authoritative surface). Displays ref counts (N watches, M domains) so the user is informed of scope.
- **Delete template** — blocked if refs exist; UI shows counts and offers "unassign from all then delete."
- **Global default toggle** — inline per row; affects new watches only.

### Watch detail — NC section

Two visual groups in the notification table:

**Library (inherited) rows** — lock icon, distinct background tint. Actions: **Unassign**, **Test**. No direct edit.

- Clicking edit shows an inline friction banner: *"This NC is shared with N watches. Changes affect all of them."* Two choices: **Edit in library** (navigates to `/notifications/{template_id}`) and **Copy to local** (creates a `watch_notification_config` with same values, removes the `watch_nc_ref`).

**Local rows** — current UX unchanged (edit, test, toggle, delete). A **Copy** action is also available on local rows, duplicating them as a new local NC on the same watch.

**Add button** — expands to two options: **Add local NC** (current flow) and **Assign from library** (picker of templates not yet assigned to this watch).

### Domain detail — new NC defaults section

New section listing templates in `domain_nc_refs`. Actions: **Remove from domain defaults**. **Add domain default** picker selects from the template library.

Informational note: *"These templates are automatically assigned to new watches created under this domain."*

## Dispatch

`dispatch_event_notifications(session, event)` unions two sources:

1. `watch_notification_configs` where `watch_id = event.watch_id` and `is_active = true`
2. `notification_templates` joined via `watch_nc_refs` where `watch_id = event.watch_id` and `template.is_active = true`

A `DispatchCandidate` dataclass normalises both result sets (apprise_url, channel_hint, events, title, source: `local|template`). Sending logic unchanged. Source type carried through for audit logging.

## Audit Events

New event types added to `WatchEventType`:
- `NOTIFICATION_TEMPLATE_CREATED`, `UPDATED`, `DELETED`, `TESTED`
- `WATCH_NC_ASSIGNED`, `WATCH_NC_UNASSIGNED`
- `DOMAIN_NC_DEFAULT_ADDED`, `DOMAIN_NC_DEFAULT_REMOVED`

Existing `NOTIFICATION_CONFIG_*` events remain for `watch_notification_configs`.

## Error Handling

- **Delete blocked:** 409 if refs exist; UI shows ref counts, offers unassign-all convenience.
- **Decryption failure on template:** OOB flash, URL field cleared; user must re-enter.
- **Auto-assignment failure on Watch create:** non-fatal; logged as warning; watch creation succeeds.
- **Test on template:** same test pattern, scoped to template (no specific watch context required).

## Testing Strategy

- Unit: `dispatch_event_notifications` with both local and template NCs; assert both fire.
- Unit: auto-assignment on watch create — global defaults, domain defaults, both, neither.
- Unit: delete-blocked 409 when refs exist; succeeds after unassign-all.
- Integration: full watch-create flow asserts `watch_nc_refs` populated correctly.
- Integration: template edit propagates (next dispatch picks up updated URL).

## Out of Scope

- Retroactive application of global/domain defaults to existing watches (future bulk-assign action if ever needed).
- Template versioning or change history.
- Per-assignment event-override (assignments inherit the template's events list).
