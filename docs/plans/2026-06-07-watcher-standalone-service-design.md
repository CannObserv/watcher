# Watcher as a Standalone Change-Detection Service

**Date:** 2026-06-07
**Supersedes:** #184 (archiver-client v4.0.0 compat — resolved by architecture)

## Goal

Decouple Watcher from the Archiver SDK at runtime, transforming it into a
general-purpose URL change-detection and notification service. Archiver becomes
a *client* of Watcher (control plane, pushing WatchedItem definitions via
Watcher's API) rather than a runtime dependency Watcher calls during every
pipeline cycle. Non-Archiver clients can create WatchedItems with a URL and
extraction specs directly, with no knowledge of Archiver.

## Context

Archiver v4.0.0 (archiver#48) removed fragment InfoSources, the `role` column
on `info_item_sources`, and the `source_spec` singular field — breaking changes
that touched Watcher's pipeline, binding-partition logic, and test fixtures.
Rather than do mechanical compat work that would immediately be thrown away, we
are redesigning toward the target architecture where Watcher stores all
runtime-critical state locally and the Archiver SDK is only used for outbound
content delivery (SourceRevision posting).

The relationship inversion also reflects a broader platform direction: Archiver
is becoming the control plane for sibling services. Watcher's administrative
dashboard becomes a notification-config and health surface; InfoItem/InfoSource
management lives entirely in Archiver's UI.

## Approved approach

Two phases (Phase C — Redis state sync — explicitly deferred; Archiver will
propagate changes via its control-plane API calls instead).

### Phase A — Pipeline decoupling

Remove the Archiver SDK from Watcher's runtime pipeline. All information the
pipeline needs (URL, extraction specs) is stored locally on WatchedItem.
Ships independently and closes #184 as resolved-by-architecture.

### Phase B — Control plane inversion

Archiver calls Watcher's API to create, update, and archive WatchedItems.
Authentication mechanism added so Archiver can act as a trusted caller. Watcher
dashboard stripped of InfoItem-coupled routes (picker, binding tree). #184
cleanup complete.

---

## Data model

### WatchedItem — changes

**Gains:**

| Column | Type | Notes |
|---|---|---|
| `effective_url` | TEXT NOT NULL | Moved from Watch; the probed URL; one per monitoring target |
| `source_specs` | JSONB[] NOT NULL | Extraction fallback chain; `[0]` is primary |
| `archiver_info_source_id` | ULID nullable | Set by Archiver at create time; drives drain worker; null for non-Archiver WatchedItems |

**Changes:**

- `info_item_id` → nullable. Partial unique index `WHERE info_item_id IS NOT NULL`
  preserves one-WatchedItem-per-InfoItem guarantee for Archiver-backed items.
- `health_status`, `last_checked_at`, `last_changed_at` **move here from Watch**.
  These are fetch-unit concerns (the pipeline runs per WatchedItem), not
  per-subscription concerns.

### Watch — slimmed to a subscription record

**Drops:**
- `target_info_source_id` — sub_aspects are now separate InfoItems, each with
  their own WatchedItem. The concept no longer exists in Watcher.
- `info_item_id` — redundant via Watch → WatchedItem → optional InfoItem.
- `effective_url` — moves to WatchedItem.
- `health_status`, `last_checked_at`, `last_changed_at` — move to WatchedItem.

**Retains:** `id`, `watched_item_id`, `name`, `description`, `is_active`,
`is_archived`, `tags`, `content_type`, notification template overrides.

Semantic framing: **WatchedItem = "Watcher is monitoring this URL." Watch = "I
am subscribed to be notified when it changes."** Multiple subscribers (teams,
users, notification targets) can independently pause or configure their Watch
without affecting others' subscriptions or the fetch cycle itself.

### New: `change_revisions` table

Watcher's own change history — one row per detected change per WatchedItem.
Replaces the Archiver-coupled `source_revision_id` as the canonical change
reference for notification payloads.

| Column | Type | Notes |
|---|---|---|
| `id` | ULID PK | Watcher-allocated; stable external reference |
| `watched_item_id` | ULID FK | → `watched_items.id` |
| `content_fingerprint` | TEXT NOT NULL | SHA-256 of extracted content |
| `captured_at` | TIMESTAMPTZ NOT NULL | When the change was detected |
| `content_size_bytes` | BIGINT nullable | |
| `archiver_revision_id` | ULID nullable | Back-populated when/if forwarded to Archiver |
| `schema_version` | INT NOT NULL | Forward-compat |

Index: `(watched_item_id, captured_at DESC)` — serves both the pipeline
fast-path (last fingerprint lookup) and the revisions list endpoint.

### Dropped: `last_known_revisions`

The per-`info_source_id` fingerprint cache is superseded by `change_revisions`.
The pipeline fast-path queries `change_revisions` for the latest fingerprint
per WatchedItem. No denormalized fingerprint column on WatchedItem — the query
is cheap and avoids a second source of truth.

### Renamed/rekeyed: `pending_source_revisions` → `pending_archiver_sync`

Rekeyed from `info_source_id` to `change_revision_id` + `watched_item_id`.
Rows created only when `watched_item.archiver_info_source_id IS NOT NULL`.
Non-Archiver WatchedItems never touch this table.

---

## Pipeline

### Removed entirely

`src/core/watches/info_item_fetch.py` — `fetch_info_item_bindings`,
`InfoItemBindings`, `InfoSourceProto`, `SourceSpecProto` — deleted. The Archiver
SDK import in `pipeline.py` and `get_archiver_client()` in `tasks.py` are
removed from the pipeline path. After Phase A, the only remaining
`ArchiverClient` usage is the drain worker.

### `check_watched_item` simplified

Old: load WatchedItem → call Archiver for bindings → extract URL from bindings → fetch.

New: load WatchedItem → read `watched_item.effective_url` → fetch. One fewer
network round-trip per cycle.

`health_status`, `last_checked_at`, and `last_changed_at` updated on WatchedItem
directly.

### `process_watched_item` — one extraction per cycle

Previously iterated over primary + cross_checks + sub_aspects bindings. Now:
one extraction target per WatchedItem, one extraction per cycle.

**Fallback chain (v1):** try `source_specs[0]`; if extraction yields zero
content, try `[1]`, `[2]`, etc. If all specs yield nothing, treat as a fetch
error. "Significant variance" detection (the cross_check role's former purpose)
is deferred.

### Change detection and `ChangeRevision` creation

When the freshly-computed fingerprint differs from the latest `change_revisions`
row for this WatchedItem:

1. Write scratch file (unchanged).
2. Insert `ChangeRevision` — `id`, `watched_item_id`, `content_fingerprint`,
   `captured_at`.
3. If `watched_item.archiver_info_source_id IS NOT NULL` → insert
   `pending_archiver_sync` row.
4. Dispatch `CHANGE_DETECTED` to **all** active, non-archived child Watches.
   Notification payload carries `change_revision_id` always;
   `archiver_revision_id` additionally once back-populated.
5. Update `watched_item.last_changed_at`.

### Drain worker

Structurally identical to today's but rekeyed. Each outbox row carries
`change_revision_id` + `watched_item_id`. Worker loads
`watched_item.archiver_info_source_id`, calls
`client.post_source_revision(info_source_id=archiver_info_source_id, ...)`,
back-populates `change_revisions.archiver_revision_id`, deletes outbox row.

`_resolve_sub_aspect_watch` — the current hack that locates a Watch from an
`info_source_id` — deleted. Watches are loaded from the WatchedItem directly.

---

## API surface

### `POST /api/v1/watched-items` (and dashboard equivalent)

Accepts `{url, source_specs, name, info_item_id?, archiver_info_source_id?}`.
Probes `url` at create time for `effective_url` and `domain_name` — same probe
logic as today. `info_item_id` and `archiver_info_source_id` are optional;
omitting both creates a fully standalone WatchedItem with no Archiver linkage.

### `POST /api/v1/watches`

`target_info_source_id` removed from `WatchCreate`. No Archiver validation at
create time. Watch creation is a pure subscription operation — the WatchedItem
must already exist and not be archived.

### New: `GET /api/v1/watched-items/{id}/revisions`

Paginated `ChangeRevision` rows — fingerprint, captured_at, content_size_bytes,
archiver_revision_id if present. Non-Archiver clients use this as their change
history without needing to query Archiver.

### Watch lifecycle routes (PATCH, DELETE)

Currently call `resolve_watch_url` → Archiver SDK. New:
`watch.watched_item.effective_url` is a local join. No SDK call.

### Dashboard routes removed (Phase B)

- `GET /info-items/search`
- `GET /info-items/{id}/binding-tree`
- InfoItem picker partial templates and JS
- Binding tree on WatchedItem detail page

Dashboard becomes: WatchedItem list/detail (health, last checked, revision
history), Watch subscription management, notification template CRUD, audit log.

---

## Out of scope

- **Phase C (Redis state sync):** Deferred. When Archiver changes a WatchedItem's
  URL or specs (e.g. InfoSource succession, spec update), it will call Watcher's
  API directly (PATCH `/watched-items/{id}`) rather than publishing a bus event
  for Watcher to consume.
- **Selector variance / cross_check replacement:** The fallback chain's v1 rule
  (zero-content → try next spec) covers the regression-detection use case
  minimally. A richer variance metric is a future concern.
- **Multi-spec targeting:** Each Watch subscribes to the WatchedItem's single
  extraction result. Per-Watch spec overrides are not in scope.
- **Content diffing API:** `change_revisions` stores fingerprint and size; full
  text retrieval via scratch files is not exposed in this phase.
