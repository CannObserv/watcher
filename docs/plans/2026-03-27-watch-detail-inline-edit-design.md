# Watch Detail Inline Edit & Danger Zone

**Date:** 2026-03-27
**Issue:** TBD

## Goal

Apply the Domain detail screen's inline edit/save/cancel UI/UX patterns to the
Watch detail screen. Replace the separate edit page with per-field inline editing,
add an Active/Inactive status toggle, and introduce Archive/Restore/Delete workflows
via a Danger Zone section.

## Approved Approach

### Page Layout (top → bottom)

1. **Header** — watch name + URL, no action buttons
2. **Details section** — inline-editable fields in divider-based card:
   - Status (toggle + badge: Active/Inactive/Archived; disabled when archived; "Restore" inline when archived)
   - Name (text)
   - URL (text)
   - Content Type (read-only — changing mid-life breaks snapshot diffing)
3. **Schedule section** — Check Interval (text, from `schedule_config.interval`)
4. **Fetch Config section** — content-type-aware fields:
   - *Shared:* Timeout (number), Headers (textarea/JSON), Ignore Patterns (textarea, one regex/line)
   - *HTML:* CSS Selectors, Exclude Selectors, Dynamic ID Patterns (all textarea, one/line), Strip Boilerplate (toggle)
   - *PDF:* Skip Empty Pages (toggle)
   - *File:* File Format (select csv/xlsx), Chunk Row Size (number), Sort Columns (textarea, one/line), Sheet Name (text)
5. **Temporal Profiles** — unchanged (read-only)
6. **Notifications** — unchanged (read-only)
7. **Change History** — unchanged (HTMX auto-refresh)
8. **Metadata footer** — ID, Created, Updated
9. **Danger Zone** — Archive (if not archived) / Delete permanently (if archived)

### Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/watches/{id}/field/{field}?mode=view\|edit` | Serve field partial |
| POST | `/watches/{id}/field/{field}` | Update single field, return view-mode partial |
| POST | `/watches/{id}/toggle-active` | Toggle active status, return toggle partial |
| POST | `/watches/{id}/archive` | Set `is_archived=True`, `is_active=False` |
| POST | `/watches/{id}/restore` | Clear `is_archived`, stay inactive |
| DELETE | `/watches/{id}` | Delete (requires archived; already exists) |

### Removed Routes

- `GET /watches/{id}/edit` — separate edit form page
- `POST /watches/{id}/edit` — edit form submission
- `POST /watches/{id}/deactivate` — replaced by toggle

### Removed Files

- `src/dashboard/templates/pages/watch_form.html`

### Field Metadata

`WATCH_FIELD_META` dict keyed by field name, with:
- `label`, `type` (text/number/textarea/select/toggle/readonly), `hint`, `cast` function
- `source`: `"column"` | `"schedule_config"` | `"fetch_config"` — tells route handler where to write
- `content_types`: optional list restricting field to specific watch content types

### Status Toggle

Checkbox-based toggle (matching Power Map pattern):
- Auto-saves via HTMX POST to `/watches/{id}/toggle-active`
- Three badge states: Active (green), Inactive (gray), Archived (red)
- Disabled when archived; "Restore from archive" button appears inline

### Archive/Delete Enforcement

- Delete requires `is_archived=True` (409 otherwise)
- Archive sets `is_archived=True` + `is_active=False`
- Restore clears `is_archived=False`, watch stays inactive

### No Migration Needed

`is_archived` already exists on the Watch model. All edits target existing columns or JSONB keys.

## Key Decisions

1. **Inline editing replaces separate edit page** — edit page and routes removed entirely
2. **Content type is read-only** — changing it mid-life breaks snapshot chain/diffing
3. **Fetch config fields are content-type-aware** — only relevant fields shown per watch type
4. **Restore keeps watch inactive** — avoids accidentally re-enabling archived watches
5. **Field partial per entity** — `watch_field.html` rather than generalizing `domain_field.html` prematurely

## Out of Scope

- Generalizing field partials across Domain/Watch (future refactor)
- Inline editing of Temporal Profiles or Notification Configs
- Batch/card-level edit mode
