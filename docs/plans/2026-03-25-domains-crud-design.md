# Domains CRUD — Design

## Goal

Full CRUD for domains in the dashboard: list view with search/filter/pagination, detail/edit view with inline-editable fields, create via URL probe, and two-step archive → delete lifecycle.

## Data Model Changes

Add two fields to `Domain`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `notes` | `Text`, nullable | `None` | Operator annotations |
| `archived_at` | `DateTime(tz)`, nullable | `None` | `None` = active; set = archived |

Status is derived (not stored):
- **Backoff** — `current_interval > min_interval`
- **Archived** — `archived_at is not None`
- **Active** — everything else

Archived domains excluded from rate-limiter sync. One Alembic migration.

## URL Structure

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/domains` | List view |
| `GET` | `/domains/new` | Create form |
| `POST` | `/domains` | Create (probe URL → extract domain) |
| `GET` | `/domains/{name}` | Detail/edit view |
| `POST` | `/domains/{name}` | Update individual fields |
| `POST` | `/domains/{name}/archive` | Set `archived_at` |
| `POST` | `/domains/{name}/restore` | Clear `archived_at` |
| `POST` | `/domains/{name}/delete` | Hard delete (archived + zero watches only) |
| `GET` | `/partials/domains-table` | HTMX table partial (search, filter, paginate) |

All mutation routes provide non-HTMX redirect fallback.

## List View

**Search/filter bar** (`.filter-card` pattern):
- Text input: search by domain name (substring match)
- Status filter pills: All / Active / Archived / Backoff
- Default filter: Active

**Table columns:**

| Column | Content |
|---|---|
| Domain | Name (plain text) |
| Status | Badge: active / backoff / archived |
| Watches | Count |
| Last Checked | Most recent `last_checked_at` across associated watches, or "—" |
| | Edit button → `/domains/{name}` |

Rows are not clickable (per STYLE.md). Edit button is the entry point.

**Sticky footer pagination:** Page size selector (25 / 50 / 100), prev/next/page numbers. HTMX-driven.

## Detail/Edit View

**Header:** Domain name + status badge.

**Details section** — inline-editable fields (Power Map pattern, no bulk Save):
- `min_interval` — "Minimum seconds between requests to this domain"
- `max_concurrency` — "Maximum simultaneous requests allowed"
- `decay_window` — "Seconds before backoff interval decays toward minimum"
- `notes`

**Watches section** — table of associated watches:
- Columns: Name (link to watch), Status, Last Checked
- Text search filter on watch name
- Status filter: All / Active / Inactive
- HTMX partial swap

**Metadata line** (subdued):
`Metadata · ID: {ulid} · Created: {date} · Updated: {date}`

**Danger Zone:**
- If active: Archive button
- If archived: Restore button + Delete button
- Delete enabled only when archived + zero watches
- Delete requires confirmation modal

## Create View

**Page:** `/domains/new`

Single URL input → probe logic extracts effective domain.
- Domain exists: redirect to detail view with flash
- Domain new: create with defaults, redirect to detail view

No other fields on create — user tunes config on detail view.

## Testing Strategy

**Unit tests:**
- Model: `archived_at` field, status derivation
- Context helpers: search, filter, pagination, watch count, last-checked aggregation

**Integration tests:**
- List route: default filter excludes archived, search, pagination
- Create: probe → domain created, duplicate handling
- Detail: renders sections, inline field updates
- Archive/restore/delete: state transitions, delete guard
- HTMX partials: correct swap responses

## Key Decisions

- Domains remain auto-created on 429 backoff; CRUD is additive
- Archive → delete lifecycle prevents accidental data loss
- Probe-based create reuses existing URL resolution logic
- Inline-editable fields (no bulk save) follows Power Map pattern
- `name` used in URLs (unique, human-readable)

## Out of Scope

- Bulk operations on domains
- Domain-level analytics/charts
- Cascading delete of watches (future consideration)
