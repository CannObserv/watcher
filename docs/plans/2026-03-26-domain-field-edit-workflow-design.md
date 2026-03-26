# Domain Field Edit/Save/Cancel Workflow

## Goal

Replace the auto-save-on-change behavior on the domain detail page with an explicit edit/save/cancel workflow per field. Fields start disabled; an Edit button enters edit mode; Save commits the change; Cancel discards it.

## Current State

`partials/domain_field.html` renders always-enabled inputs that POST on `change` via HTMX. No explicit save/cancel affordance (noscript fallback only).

## Approved Approach

### Field States

Each field partial renders in one of two modes:

- **View mode** (default): input/textarea is `disabled`, Edit button visible.
- **Edit mode**: input/textarea is enabled, Save + Cancel buttons replace Edit.

### Layout

**Number fields** (`min_interval`, `max_concurrency`, `decay_window`):
- Single row: label + hint (left) | disabled input + unit (center-right) | Edit button (far right)
- Edit mode: input enabled, Edit replaced by Save/Cancel pair

**Textarea** (`notes`):
- Label row: label (left) + Edit button (inline, left-aligned with textarea edge)
- Textarea below, disabled
- Edit mode: Edit replaced by Save/Cancel pair, textarea enabled

### Button Styling

- **Edit**: `btn-ghost` or small text link — unobtrusive in view mode
- **Save**: `btn-primary` (purple)
- **Cancel**: `btn-secondary` (outlined)

Per screenshot reference, Save and Cancel are side-by-side with Save on the left.

### HTMX Interaction

No custom JS required. All transitions are HTMX partial swaps:

1. **Edit click**: `hx-get="/domains/{name}/field/{field_name}?mode=edit"` — returns partial in edit mode, swaps `#field-{name}`.
2. **Cancel click**: `hx-get="/domains/{name}/field/{field_name}"` — returns partial in view mode (default), swaps `#field-{name}`.
3. **Save click**: `hx-post="/domains/{name}"` with field + value — existing POST handler returns updated partial in view mode.

### Route Changes

Add a new GET endpoint to serve individual field partials:

```
GET /domains/{name}/field/{field_name}?mode=edit|view
```

Returns the `domain_field.html` partial for the requested field in the specified mode. Defaults to view mode.

### Template Changes

`partials/domain_field.html` gains a `field_mode` variable (`"view"` or `"edit"`):

- View mode: input has `disabled` attribute, Edit button shown, no hx-trigger on input
- Edit mode: input enabled, Save/Cancel shown, Save carries `hx-post`

### No-JS Fallback

Without JS, the form still works: fields are always enabled (no `disabled`), noscript Save button submits the form. The edit/save/cancel workflow is a progressive enhancement.

## Out of Scope

- Optimistic UI / loading spinners (existing `htmx-request` CSS handles this)
- Undo after save
- Batch editing multiple fields
- Validation error display changes (existing flash messages suffice)
