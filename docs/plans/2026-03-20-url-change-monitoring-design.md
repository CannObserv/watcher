# URL Change Monitoring System — Design

**Date**: 2026-03-20
**Status**: Approved

## Goal

Build a web service for monitoring URLs (HTML pages, PDFs, CSV/Excel files) for changes. API-first with dashboard for visibility. Designed to handle 500–2,000 watches with intelligent scheduling, content-aware diffing, and structured change metadata.

## Prior Art & Motivation

Evaluated three existing tools:

- **urlwatch** — mature CLI, good filter composability, but no API, flat-file storage, frequent false positives
- **Klaxon** — simple PostgreSQL-backed history, but HTML-only, no API, fixed intervals, abandoned
- **changedetection.io** — most robust, has API, but overly complex, unreliable on shared VM, too focused on price monitoring

None handled non-HTML documents (PDFs) well. None supported temporal watch profiles (frequency that adapts based on proximity to known dates/events).

## Approved Approach

Monolith with async task queue (arq + Redis). FastAPI API process enqueues check jobs; arq workers execute fetching, extraction, diffing. Both run in a single process initially, separable later. PostgreSQL for structured data, local filesystem for content storage.

## Key Decisions

### Data Model

- **ULIDs** for all primary keys (sortable by creation time)
- **`watches`** — URL, content type, fetch config (selectors, exclude selectors, normalization rules), schedule config
- **`temporal_profiles`** — scheduling rules tied to external dates (event-driven, seasonal, deadline-driven). Rules defined as `{days_before, interval}` pairs with post-event actions (reduce frequency, deactivate, archive)
- **`snapshots`** — one per fetch. Stores content hash, simhash, filesystem paths to raw content and extracted text. No large content in the DB.
- **`snapshot_chunks`** — structural decomposition of each snapshot. Per-page (PDF), per-section (HTML), per-row-range (CSV). Stores hash, simhash, char count, ~500-char excerpt. Enables targeted change detection ("Page 7 changed") without loading full content.
- **`changes`** — precomputed at ingest time. References previous/current snapshots with chunk-level metadata. Never diff on the fly.
- **`audit_log`** — every system operation (watch.created, check.started, change.detected, notification.sent, etc.)
- **`domains`** — per-domain rate limiting state

### Content Storage

DB stores fingerprints and structure; filesystem stores content.

| DB | Filesystem |
|---|---|
| Hashes, simhashes, chunk metadata | Raw content (PDF, HTML, CSV) |
| 500-char excerpts per chunk | Full extracted text |
| Precomputed change records | Detailed diffs (optional) |

Raw content archived untouched — PDF metadata (author, creation date) preserved in full. Diffing operates on normalized extracted text only.

Storage path: `data/snapshots/{watch_id}/{snapshot_id}.{ext}`

`StorageBackend` protocol with `LocalStorage` implementation. Abstraction in place for future GCS backend.

### Temporal Watch Profiles

Three profile types:
1. **Event-driven** — meeting agendas, hearings. Ramp up frequency as event approaches, drop off after, eventually retire.
2. **Seasonal** — legislative sessions. Active during session, dormant during recess.
3. **Deadline-driven** — license renewals, filing deadlines. Intensify as date approaches.

Example profile:
```json
{
  "profile_type": "event",
  "reference_date": "2026-04-15",
  "rules": [
    {"days_before": 30, "interval": "6h"},
    {"days_before": 7, "interval": "1h"},
    {"days_before": 1, "interval": "15m"}
  ],
  "post_action": "reduce_frequency"
}
```

### False Positive Mitigation

**Extraction-time normalization:**
- Whitespace normalization (all content types)
- CSS/XPath selector targeting + exclusion selectors within selected content
- Dynamic ID stripping (configurable patterns, e.g. Squarespace `data-block-id`)
- Boilerplate exclusion (`<header>`, `<footer>`, `<nav>`, `<script>`, `<style>`)
- CSV sort normalization (optional, by key columns)

**Diff-time scoring:**
- SimHash similarity scoring on changed chunks (informational, not a notification gate)
- Minimum change threshold (configurable)
- Per-watch ignore patterns (regex)

**Notifications default to all changes.** Significance score is metadata, not a filter.

**PDF metadata preserved** in archived copies. Metadata excluded from diff comparison only.

### Fetching

Progressive strategy — start simple, escalate as needed:
1. **HTTP** (`httpx` async) — default
2. **Headless browser** (Playwright) — JS-heavy sites
3. **WebRecorder** — sophisticated capture/archival

Per-domain rate limiting via Redis: max concurrency, minimum interval between requests, automatic backoff on 429s. All independent watches to a domain coordinated to avoid abuse.

Each watch has a fetcher preference; worker escalates on failure. Fetcher used is recorded on the snapshot.

### Architecture

```
FastAPI App (API routes + schemas)
       │
   PostgreSQL
       │
arq Workers (Redis)
  ├── Scheduler (evaluate due watches, enqueue jobs)
  ├── Fetcher (fetch → extract → chunk → diff → store)
  └── Notifier (dispatch to configured channels)
```

Source layout:
- `src/api/` — FastAPI routes and schemas
- `src/core/models/` — SQLAlchemy models
- `src/core/fetchers/` — Pluggable fetcher protocol (http, browser, webrecorder)
- `src/core/extractors/` — Content → text + chunks (html, pdf, csv)
- `src/core/differ.py` — Chunk-level hash comparison + SimHash
- `src/core/storage.py` — StorageBackend protocol + LocalStorage
- `src/core/scheduler.py` — Temporal profile resolution
- `src/core/rate_limiter.py` — Per-domain enforcement (Redis)
- `src/workers/` — arq task definitions

### Testing Strategy

| Layer | Test type | What's tested |
|---|---|---|
| Extractors | Unit | HTML/PDF/CSV → chunks + hashes |
| Differ | Unit | Chunk comparison, SimHash scoring |
| Normalizers | Unit | Whitespace, ID stripping, selector inclusion/exclusion |
| Storage | Unit | LocalStorage read/write/path generation |
| Scheduler | Unit | Temporal profile resolution, edge cases |
| Rate limiter | Integration | Redis-backed concurrency/interval |
| Fetchers | Integration | HTTP fetcher against test server |
| Worker orchestration | Integration | Full pipeline with real PostgreSQL |
| API routes | Integration | FastAPI TestClient with real DB |

## Implementation Phases

1. **Foundation** — models, migrations, LocalStorage, Watch CRUD API, audit log
2. **Content pipeline** — extractors (HTML, PDF, CSV), differ, snapshot/chunk storage
3. **Fetching & scheduling** — HTTP fetcher, rate limiter, arq workers, basic scheduler
4. **Temporal profiles** — profile model + API, profile resolution, post-event actions
5. **Change detection & querying** — Changes API, structured metadata, audit log queries
6. **Notifications** — framework + email, webhook, Slack channels
7. **Advanced fetching** — Playwright, WebRecorder, adaptive escalation
8. **Dashboard** — web UI for watch management and change visibility

Each phase is independently deployable and testable.

## Out of Scope

- Price monitoring / e-commerce features
- Entity relationship tracking (watch ↔ organization linkage)
- JSON/XML API endpoint monitoring (future)
- GCS storage backend implementation (abstraction only)
- Multi-user / auth (single-operator for now)
