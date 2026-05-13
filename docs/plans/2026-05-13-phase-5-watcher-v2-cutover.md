---
title: Phase 5 — Watcher v2 Cutover (SourceRevisions in Archiver)
date: 2026-05-13
status: approved (design)
issue: https://github.com/CannObserv/watcher/issues/156
anchors:
  - /home/exedev/archiver/docs/plans/2026-05-08-archiver-v2-architecture-design.md
  - /home/exedev/archiver/docs/plans/2026-05-08-phase-4-archiver-v2-implementation.md
---

# Phase 5 — Watcher v2 Cutover

## Goal

Refactor Watcher to produce `SourceRevisions` in Archiver instead of locally-stored `Snapshot`s. After this phase, Archiver is the system-of-record for content identity; Watcher owns scheduling, fetch execution, subscription tracking, and notification dispatch.

Clean cutover: no compat shim, no dual-write, no `Snapshot` table.

## Non-goals

- Replicator stand-up (Phase 6) — separate sibling repo.
- WordPress cache integration (Phase 7) — separate WP design.
- Authoring CLI (Phase 8) — defer until operator demand.
- Cadence-conflict UI affordances beyond a derived-cadence display on root Watches.
- Redirect-conveyance workflow from Watcher to Archiver — tracked in #157.

## Anchored decisions

These are the load-bearing choices that scope the rest of the design. Recorded so future readers know *why* the design has the shape it does.

1. **Producer/consumer split.** Archiver owns `info.changes`. Watcher does not produce, consume, or bridge that stream. Watcher learns "a new SourceRevision exists" from its own POST result.
2. **Watch reshape.** Root and fragment Watches are equivalent rows on a single `watches` table, bound to `info_source_id`. Watching the root drives fetch; fragment Watches optionally narrow subscription identity. No separate fragment-subscription entity.
3. **Cadence reconciliation.** Effective root tick cadence = `min(root.schedule, min(fragment_schedules))`. Root + every fragment Watch evaluates on each tick. Surface the derived cadence in the root Watch UI.
4. **Local content persistence dropped.** `Snapshot`, `Change`, `simhash`, `differ.py`, chunk-level diff plumbing all removed. Bytes live only in scratch path during a fetch operation. Notifications become page/fragment-level only, keyed by fingerprint shift.
5. **Outbox guarantees delivery to Archiver, not to a bus.** New table `pending_source_revisions` buffers POSTs when Archiver is unreachable. Drain worker retries with backoff. Notification dispatch fires on inline POST success **and** on outbox drain success.
6. **Notification dispatch trigger is the successful POST**, inline or outbox-drained. Watcher already knows when it wrote a new revision; no bus consumption required.
7. **`effective_domain` resolved once at Watch creation** from `info_sources.url`. Tick-time rate-limiting reads the Watch row directly. `effective_url` tracked the same way for now; long-term workflow is #157.
8. **SDK pinned with both path-editable AND version constraint** (`>=2.1.0,<3`). Fails loudly when Archiver hits v3.
9. **Acceptance criteria stay inside Watcher's boundary.** Tests assert Watcher's POST surface, outbox state, and notification dispatch — not Archiver event emission.

---

## Section 1 — Watch reshape

### Schema changes

```sql
-- New
ALTER TABLE watches ADD COLUMN info_source_id ULID NOT NULL
  REFERENCES information.info_sources(info_source_id) ON DELETE RESTRICT;
CREATE INDEX ix_watches_info_source_id ON watches(info_source_id);

-- Dropped
ALTER TABLE watches DROP COLUMN info_item_id;
```

`effective_domain` stays on Watch. `effective_url` stays on Watch (per #157; revisit when the redirect-conveyance workflow is designed).

### Integrity invariants

- **Fragment-Watch create requires an active root Watch.** Walk `parent_info_source_id` from the target source up to root; reject with 422 if no active Watch references any source in the chain.
- **Root-Watch delete blocks when fragment Watches exist on the chain.** Override via `?cascade=true` query param archives all dependent fragment Watches in the same transaction.
- **One Watch per (`info_source_id`, `name`).** Allows alternate notification configs on the same source without duplicating the source identity.

### Cadence reconciliation

The Procrastinate scheduler computes the effective root tick as `min(root.schedule, min(fragment_schedules))` at scheduling time. On each tick:
1. Fetch root URL once.
2. Hash root extraction; compare against previous root SourceRevision's fingerprint.
3. If unchanged → fast-path skip cascade (identical bytes ⇒ identical fragments).
4. If changed → POST root revision, run cascade extraction, POST each changed fragment revision, dispatch notifications per the Watches that subscribe.

A fragment Watch's `schedule_config` participates in the min-computation but does **not** create a separate fetch — it only tightens root cadence. Surface derived cadence ("effective: every 15m, tightened by 3 fragment Watches") on the root Watch's UI.

### Migration mechanics

For each existing `Watch.info_item_id`:
1. Look up Archiver's `info_item_sources` for active `role='primary'` rows.
2. If exactly 1 → create Watch row bound to that `info_source_id`, preserve other columns.
3. If 0 → halt with operator manifest entry (orphaned Watch; needs InfoSource setup).
4. If N>1 → halt with manifest entry (operator picks which source).

One-shot script under `scripts/migrate_watches_to_v2.py`. Manifest is a JSONL file; operator resolves conflicts manually before re-running. Migration is not idempotent on a partial-completion state — script aborts cleanly and refuses to proceed past first conflict.

---

## Section 2 — Pipeline rewrite

### `src/core/info_resolver.py` surface change

Drop:
```python
async def resolve_primary(client, info_item_id, *, force_refresh=False) -> ResolvedInfoSpec
```

Add:
```python
@dataclass(frozen=True)
class ResolvedRootSource:
    info_source_id: str
    url: str
    source_spec: dict
    children: list[ResolvedFragmentSource]  # zero or more

@dataclass(frozen=True)
class ResolvedFragmentSource:
    info_source_id: str
    parent_info_source_id: str
    source_spec: dict

async def resolve_root_sources_with_children(
    client: ArchiverClient,
    info_source_id: str,
    *,
    force_refresh: bool = False,
) -> ResolvedRootSource
```

Implementation: walks `parent_info_source_id` up to root, then `list_info_sources(parent_info_source_id=root_id)` for children. Single hot-path SDK call after the parent walk; avoids N+1.

### Fetch loop

`src/workers/tasks.py:check_watch` and `src/workers/pipeline.py:_run_check_pipeline` reshape:

1. Load Watch by id.
2. `resolved = resolve_root_sources_with_children(client, watch.info_source_id)`.
3. Fetch `resolved.url` via existing fetcher infrastructure. Use `watch.effective_domain` for rate-limit bucketing.
4. Write raw bytes to `WATCHER_CACHE_DIR/<placeholder>.bin` immediately. Compute final `source_revision_id` (ULID) as a fresh allocation, then rename the file to `<source_revision_id>.bin` before POST.
5. Extract per root SourceSpec → SHA-256 over post-trim content → assemble POST body.
6. POST root revision. On success: cascade. On failure: enqueue in `pending_source_revisions`, abort cascade (cascade requires the root's `source_revision_id` from Archiver's response).
7. Cascade: for each `ResolvedFragmentSource`, extract from cached bytes → SHA-256 → POST. Each fragment is an independent outbox candidate.
8. After all POSTs land: dispatch notifications per Watch (root Watch + each fragment Watch whose source produced a new revision).

### Fast-path

Before extracting, fetch the most recent SourceRevision for the root from Archiver (`list_source_revisions(info_source_id=root_id, limit=1)`). If today's raw-content SHA-256 (computed cheaply, no extraction) matches *and* extraction would produce the same trimmed bytes — skip. **Note**: raw-bytes hash is not the SourceSpec fingerprint (which is over extracted content). Two options:
- **(a)** Fast-path on extracted-content hash only (always extract; skip POST if unchanged).
- **(b)** Fast-path on raw-bytes hash too (Watcher-local cache of "last raw hash → last extracted fingerprint"); skip extraction entirely if raw hash unchanged.

(a) is simpler and aligns with the design. Default to (a). Profile in production; if extraction cost dominates, add (b) as a Watcher-local cache.

---

## Section 3 — Temp cache + sweeper

### Scratch protocol

- Directory: `WATCHER_CACHE_DIR` (default `/var/cache/watcher/scratch/`). Watcher owns. Single-VM; both Watcher's pipeline and Replicator's fallback read locally.
- Filename: `<source_revision_id>.bin`. ULID embedded in name so the sweeper can identify the row without DB lookup.
- POST body carries `content_cache_uri = file:///var/cache/watcher/scratch/<id>.bin` + `content_cache_expires_at = now + WATCHER_CACHE_TTL_SECONDS` (default 600s).

### Sweeper

- Runs every `WATCHER_CACHE_SWEEP_INTERVAL_SECONDS` (default 60s) as a Procrastinate periodic task.
- For each file older than TTL:
  1. Extract `source_revision_id` from filename.
  2. If `EXISTS (SELECT 1 FROM pending_source_revisions WHERE … referencing this id)` → **skip delete**. The outbox still needs to reference the file when it eventually drains.
  3. Else delete the file.
  4. PATCH `/source-revisions/{id}/cache` to NULL the cache fields (best-effort; log and drop on failure — Replicator's read-failure fallback is the safety net).

The sweeper-outbox interlock guarantees a deferred POST always finds its scratch file on the eventual successful drain.

### Configuration

- `WATCHER_CACHE_DIR` (default `/var/cache/watcher/scratch/`)
- `WATCHER_CACHE_TTL_SECONDS` (default `600`)
- `WATCHER_CACHE_SWEEP_INTERVAL_SECONDS` (default `60`)

---

## Section 4 — Watcher-side outbox

### Table

```sql
CREATE TABLE pending_source_revisions (
  id                        ULID PRIMARY KEY,
  info_source_id            ULID NOT NULL,
  content_fingerprint       TEXT NOT NULL,                  -- 'sha256:<hex>'
  content_size_bytes        BIGINT NULL,
  content_media_type        TEXT NULL,
  content_cache_uri         TEXT NOT NULL,                  -- 'file:///...'
  content_cache_expires_at  TIMESTAMPTZ NOT NULL,
  captured_at               TIMESTAMPTZ NOT NULL,
  attempts                  INTEGER NOT NULL DEFAULT 0,
  last_error                TEXT NULL,
  next_attempt_at           TIMESTAMPTZ NOT NULL,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (info_source_id, content_fingerprint)
);

CREATE INDEX ix_pending_source_revisions_next_attempt
  ON pending_source_revisions(next_attempt_at)
  WHERE attempts < 10;  -- partial index keeps live queue small
```

### Drain worker

`src/workers/source_revisions_drain.py`:

- Procrastinate periodic task, runs every 60s.
- `SELECT … FROM pending_source_revisions WHERE next_attempt_at <= now() ORDER BY next_attempt_at LIMIT 100 FOR UPDATE SKIP LOCKED`.
- For each row: re-attempt `client.post_source_revision(...)`. Archiver POST is idempotent on `(source_id, fingerprint)` — retries are safe.
- On success: delete row, fire notification dispatch (same code path as inline-POST success).
- On failure: increment `attempts`, set `last_error`, `next_attempt_at = now() + backoff(attempts)`. Backoff is exponential capped at 1 hour.
- After `attempts >= 10`: leave the row, log a structured alert, stop retrying. Operator triage.

### Sweeper interaction

The sweeper's `EXISTS` check uses `info_source_id` + filename-derived ULID. The ULID in the filename is the *intended* `source_revision_id`; once Archiver accepts the POST and assigns its own ULID (or, more precisely, accepts the client-provided one — design contract), the IDs match. So the outbox row's `id` column doubles as the scratch filename's ULID. Confirm during implementation that `post_source_revision` honors client-provided ULIDs.

---

## Section 5 — Notification dispatch

### Trigger sites

Two sites, both inside Watcher:

1. **Inline POST success** in `src/workers/pipeline.py`, after a SourceRevision POST returns 200/201.
2. **Outbox drain success** in `src/workers/source_revisions_drain.py`, after a deferred POST eventually lands.

Both call the same dispatch function `dispatch_notifications_for_revision(watch, source_revision_id, …)`. Idempotency comes from the notification side (Notifier dedups on tenant + Watch + revision id).

### Per-fragment dispatch

For each fragment SourceRevision posted in a cascade, identify the fragment Watches that subscribe (`watches.info_source_id = fragment.info_source_id, is_active = true`). Each fires independently. A root Watch fires too if it subscribes (most do).

### Template var resolution

Template helpers need fragment-aware context:
- `{{ watch.url }}` resolves to root URL even for a fragment Watch (Watcher fetches at root; the URL the operator visits is still root).
- New helper `{{ source.fragment_label }}` or similar surfaces fragment identity in templates.

This is a template-layer concern; not blocking architecture but blocks the first end-to-end notification test. Capture as a task.

---

## Section 6 — Removals

Files and references to delete in this cutover:

- `src/core/changes/publisher.py`
- `src/core/changes/outbox.py`
- `src/core/changes/` (directory empty after removal)
- `src/workers/changes_drain.py`
- `src/core/models/snapshot.py`
- `src/core/models/change.py`
- `src/core/differ.py`
- `tools/info_changes_consumer.py`
- `Snapshot.simhash`, `Change.significance`, `Change.change_metadata` columns (via drop-table migration; full tables go away)
- `DRAIN_ADVISORY_LOCK_ID` constant + lifespan plumbing in `src/api/main.py`
- `CHANGES_DRAIN_INTERVAL_SECONDS` env var + reference
- `published_to_bus_at` / `bus_message_id` (on the dropped Change table)
- `src/core/simhash.py` if no remaining callers (verify with `codebase_impact` before delete)
- Test files mirroring each of the above

### Mirror to Archiver

Per AGENTS.md, `src/core/simhash.py` is one of the mirrored files. If Watcher drops it, Archiver may still use it. Check Archiver before deleting; if Archiver still imports, the file stays in Watcher's mirror (annotate as "kept for mirror parity, not imported").

---

## Section 7 — SDK pin

`pyproject.toml`:

```toml
archiver-client = { path = "/home/exedev/archiver/clients/python", editable = true, version = ">=2.1.0,<3" }
```

Verify uv accepts this combination. If not, fall back to:
- Path-editable in development (`.env`-based override or extras_require)
- Version pin in main dep table

Either way, fail loudly when Archiver hits v3.

### Error envelope migration

Per archiver v2.0.0 changelog, error handling uses a unified envelope:

```python
try:
    await client.post_source_revision(...)
except Conflict as e:
    existing_id = e.data["existing_info_source_id"]  # for example
    ...
except InformationError as e:
    logger.error("archiver error", extra={"kind": e.kind, "errors": e.errors})
    ...
```

Sweep all existing catch sites in `src/core/`, `src/workers/`, `src/api/`, `src/dashboard/`.

---

## Section 8 — Acceptance criteria

All criteria stay inside Watcher's boundary. Tests do not assert behavior in Archiver or downstream services.

- [ ] `watches.info_source_id` column added; `watches.info_item_id` dropped.
- [ ] Migration script halts on multi-primary or zero-primary items with operator manifest.
- [ ] Fragment-Watch create rejects with 422 when no active root Watch exists on the chain.
- [ ] Root-Watch delete blocks when fragments exist; `?cascade=true` archives them together.
- [ ] Effective root cadence = `min(root.schedule, min(fragment_schedules))`; root + every fragment Watch evaluates on each tick.
- [ ] Per fetch: 1 root + N fragment SourceRevisions POSTed to Archiver, idempotent on `(source_id, fingerprint)`.
- [ ] Fast-path: unchanged extracted root fingerprint skips cascade.
- [ ] Scratch path populated before POST; sweeper deletes after TTL **except** for files referenced by un-drained outbox rows; best-effort PATCH-cache-clear after delete.
- [ ] `pending_source_revisions` buffers when Archiver is unreachable; drain worker retries with backoff capped at 1 hour; gives up after 10 attempts with alert.
- [ ] Notifications dispatched per Watch via Notifier SDK at both inline POST success and outbox drain success; root + fragment Watches dispatch independently.
- [ ] `Snapshot`, `Change`, `simhash` (if no Archiver mirror need), `differ.py`, and `info.changes` producer plumbing removed; no references remain in `src/`, `tests/`, or `tools/`.
- [ ] SDK pin: path-editable + `version = ">=2.1.0,<3"`; error envelope migrated to `Conflict` + `InformationError.kind/errors/data`.
- [ ] `effective_domain` resolved once at Watch creation from `info_sources.url`; not re-derived per tick.
- [ ] `effective_url` continues to be tracked alongside `effective_domain` (long-term workflow in #157).
- [ ] Watcher tests + lint pass; `uv run pytest -m integration` green; CHANGELOG entry summarizes the v2 cutover.

---

## Risks + open questions

- **Multi-primary-source items in migration.** Manifest-and-halt is conservative; an operator may want auto-pick-first-by-created_at. Decide before running the one-shot script in production.
- **Fragment-template var resolution.** Template helpers for per-fragment context need design — not architecture, but blocks the first notification dispatch test if unowned.
- **Client-provided ULID for `source_revision_id`.** The scratch-file/outbox design depends on Watcher allocating the ULID before POST. Verify Archiver's `post_source_revision` honors a client-supplied id; if it returns its own, scratch filename has to be a placeholder + rename after POST, which loses the sweeper-outbox interlock cleanliness. Confirm during implementation start.
- **`simhash.py` mirror policy.** Per AGENTS.md mirror discipline, the file may need to stay even if Watcher stops using it. Coordinate with Archiver before deleting.
- **Redirect conveyance to Archiver (#157).** Watcher learns about new redirect targets first. Conveyance workflow is its own design effort; this plan keeps `effective_url` on Watch as an interim measure.

---

## Sequencing

Suggested task order for the implementation plan:

1. SDK pin update + error envelope migration (smallest blast radius; lands first).
2. `info.changes` producer removal (no functional change once Archiver is the producer; safe to land independently).
3. `pending_source_revisions` table + drain worker scaffolding (no callers yet).
4. `resolve_root_sources_with_children` SDK wrapper + tests.
5. Watch reshape migration script + integrity invariants (delete-block, fragment-Watch-create-checks).
6. Pipeline rewrite: scratch path, POST, cascade.
7. Sweeper with outbox interlock.
8. Notification dispatch trigger relocation (inline + drain).
9. `Snapshot` / `Change` / `differ` deletions.
10. End-to-end integration test + CHANGELOG entry.

Per-task TDD breakdown to follow in a `writing-plans` pass.
