# Phase 4: the `content.fetch` producer — design

**Status:** design for review. **Issue:** #241 (intake), with #245 (politeness wire, shipped),
replicator#9/#10/#11 (contract additions, shipped), replicator#25 (adaptive backoff, open).

**Normative contracts (link, don't copy):**
[content-fetch-issuer-contract.md](https://github.com/CannObserv/replicator/blob/main/docs/contracts/content-fetch-issuer-contract.md)
· [replicator-boundaries.md](https://github.com/CannObserv/replicator/blob/main/docs/contracts/replicator-boundaries.md).
Built against co-core **v0.7.7**.

## Problem

Watcher's scheduled check path fetches origin content itself (`check_watched_item` →
`HttpFetcher` → `process_watched_item`). The cluster strategy (archiver#72) moves all
fetching to Replicator: Watcher decides *when and what*, issues a `ContentFetchCommand`,
and reacts to the returning `BlobAvailableEvent` / `FetchFailedEvent`. Single fetcher ⇒
the fingerprint-parity problem dissolves.

The move splits one synchronous function into an asynchronous round-trip with no latency
bound, at-least-once delivery in both directions, and (for stalls and undecodable frames)
silence as the only failure signal. The issuer contract's seven MUSTs cover the wire; this
design covers Watcher's side: correlation state, scheduling discipline, and failure
surfacing.

## Shape

### One table: `fetch_commands`

Outbox, pending map, and inbox in one row, keyed by the correlator:

```
fetch_commands
  command_id        TEXT PK          -- ULID, minted per fetch occasion (MUST-1)
  intent_id         TEXT NOT NULL    -- lineage across re-issues of one intent
  watched_item_id   ULID NOT NULL    -- ON DELETE CASCADE (joins the #210 child list)
  url               TEXT NOT NULL    -- what we asked for (debug; never a key, MUST-3)
  status            TEXT NOT NULL    -- pending_publish | in_flight | succeeded |
                                     -- failed | superseded | expired
  issued_at         TIMESTAMPTZ NOT NULL
  published_at      TIMESTAMPTZ      -- NULL until the XADD is confirmed
  reissue_count     INT NOT NULL DEFAULT 0
  -- fact fields, upserted by the consumer (MUST-4; per-emission keys mean
  -- several distinct facts per command — last terminal wins):
  fact_at           TIMESTAMPTZ
  content_fingerprint TEXT           -- raw-bytes identity (Replicator's), NOT
                                     -- watcher's extracted-text fingerprint
  blob_uri          TEXT
  size_bytes        BIGINT
  media_type        TEXT
  content_type_raw  TEXT
  final_url         TEXT
  status_code       INT
  failure_reason    TEXT             -- fetch_failed.reason token
  failure_detail    TEXT
  applied_at        TIMESTAMPTZ      -- pipeline ran over the blob
```

Partial index on the open statuses (`pending_publish`, `in_flight`) by
`watched_item_id` — the schedule-tick gate and the reaper both scan only open rows.

### Issue path (replaces the fetch half of `check_watched_item`)

`issue_fetch(watched_item_id)` — a Procrastinate task, enqueued by `schedule_tick` and
check-now:

1. Guards (unchanged): active, not archived, not domain-suspended, has `effective_url`.
2. **Persist first** (MUST-2): INSERT `fetch_commands` row (`pending_publish`,
   fresh ULID `command_id`, fresh `intent_id`), **commit**.
3. XADD via `to_wire(ContentFetchCommandEmit)` with
   `headers={"user-agent": "watcher/0.1.0"}` (byte-continuity across the cutover —
   the whole point of replicator#11) — then mark `in_flight` + `published_at`, commit.
4. Crash between 2 and 3 → a `pending_publish` row the publisher sweep re-publishes
   **under the same `command_id`**; Replicator's dedupe makes the duplicate XADD a no-op.
5. Redis `OutOfMemoryError` (the broker is capped, archiver#129) is retryable — the row
   stays `pending_publish` and the sweep retries it.

**No validator headers in Phase 4** — a body-less 304 dead-letters (replicator#17); we
*store* `etag`/`last_modified` off the fact for the day #17 closes but never send them.

**Politeness at issuance:** `acquire_for_domain`'s sleep is retired here — enforcement is
Replicator's (#245 shipped the numbers; replicator#25 the adaptive part). The issue path
does not sleep, and `report_rate_limited_for_domain` has no caller on this path.

### Consume path (new lifespan task, beside the config poller)

`content.blobs` consumer — `AsyncBusConsumer(topic=content.blobs, group="watcher")`,
one in-process asyncio task (the single-process topology holds; the group has one member).
Per message, branch on payload type:

- **`BlobAvailableEvent`** → upsert fact fields onto the row by `command_id`
  (MUST-4/MUST-5: never dedupe on fingerprint; unknown `command_id` → log + ack —
  a fact for a map entry we lost is discardable by contract), commit, **ack**, then
  defer `apply_blob(command_id)`.
- **`FetchFailedEvent`** → branch `terminal` first; terminal → `status='failed'` +
  reason/detail upserted, commit, ack, defer `apply_failure(command_id)`.
  Non-terminal facts (none emitted today, replicator#9 §3) refresh `fact_at` only.
- Undecodable frame → skip past (no correlation obligation on a fact stream we read
  with our own group; mirrors Replicator's policy-reader posture).

Keeping the handler to an upsert+ack keeps at-least-once semantics inside one Postgres
transaction; the heavy work rides Procrastinate as usual.

### Apply path

`apply_blob(command_id)` — Procrastinate task:

1. Load row + item. **Ordering guard:** if a *newer* command for this item has already
   applied (`issued_at` comparison), mark `superseded` and stop — two in-flight commands
   (reaper re-issue racing a recovered original) must not fingerprint-flap A→B→A and fire
   a phantom CHANGE_DETECTED. The bus guarantees no ordering.
2. Read the bytes at `blob_uri` (host-local `file://`, MUST-7 — read promptly, never
   schedule against the 7-day TTL). Open failure → treat as a timed-out intent:
   re-issue under a fresh `command_id`, same `intent_id`, row → `expired`.
3. Seed `content_media_type` from `content_type_raw` (None-aware — the raw header
   preserves the #168 "absent ≠ octet-stream" distinction).
4. Run today's pipeline unchanged: `process_watched_item(session, item, raw_content=…)`
   — extraction, watcher-fingerprint, ChangeRevision, notifications, archiver sync.
5. Bookkeeping formerly in `check_watched_item`: `last_checked_at`, health OK +
   WATCH_RECOVERED transition, audit (`CHECK_SNAPSHOT_CREATED`/`CHECK_NO_CHANGE`),
   `applied_at`, `status='succeeded'`.
6. `final_url` ≠ requested url → audit event only (feeds #157; no auto-rewrite of
   `effective_url` — Archiver stays authoritative).

`apply_failure(command_id)` — maps the reason token onto today's failure path
(`_record_check_failure`): ERROR health, fresh `last_checked_at`, `CHECK_FETCH_FAILED`
audit (+ status_code), WATCH_ERROR on the OK→ERROR transition.

### Scheduling discipline — the expensive hazard

`schedule_tick` currently re-enqueues on a stale `last_checked_at`. Post-cutover,
`last_checked_at` only advances when a fact **arrives**, so without a gate a silently
failed item is re-issued every tick — 1,440 real origin fetches/day for one 404ing item,
invisible from Replicator's side. **Gate: skip any item with an open `fetch_commands`
row.** The open row *is* the in-flight marker; the reaper is what closes it.

### Reaper (periodic task)

Closes what nothing else will (MUST-6 survives `fetch_failed` — stalls and undecodable
frames are still silent). Keyed on **signal age** — `coalesce(fact_at, published_at)` —
so a fact whose apply job died cannot shield its row forever (CR-2): a stale `in_flight`
row that *holds a blob fact* gets its apply **re-deferred** (the bytes exist; refetching
would waste an origin request); everything else follows the expire/re-issue path below.

- Open rows whose last signal is older than `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS` (default **1800** —
  deliberately generous; Replicator's reclaim cadence is an operator knob on another
  host, and a pinned 60s would re-issue under live retries) → `expired`, re-issue fresh
  `command_id` / same `intent_id`, `reissue_count + 1`.
- `reissue_count` ≥ `WATCHER_FETCH_MAX_REISSUES` (default **3**) → stop re-issuing:
  ERROR health + WATCH_ERROR (`reason="fetch_timeout"`), row `failed`. The item then
  re-enters normal scheduling (the gate lifts), so recovery is automatic when the origin
  or Replicator recovers.

## What this retires, and what it strands

- `HttpFetcher` leaves the scheduled path but **stays** for `probe_url` *until* async
  create lands (below) — then probe-by-command is the only fetch left and the adapter
  narrows to nothing on the hot path.
- `DomainRateLimiter`: acquire/backoff/decay retire from the check path;
  `Domain.current_interval`, `last_request_at`, `decay_window` stop being written
  (columns stay; removal is a later cleanup once replicator#25 proves out). Dashboard
  `in_backoff` presentation is removed in the same change (#245 follow-through).
- `WATCHER_CACHE_*` is **not** retired (contra the epic's shorthand): scratch still
  feeds `pending_archiver_sync` with *extracted* content. Retiring it needs Archiver to
  accept raw-blob URIs — a separate Archiver-side conversation.

## `probe_url` → async create (decided)

Create/PATCH stops probing inline. New item enters `health_status='probing'` with
`effective_url=NULL`; a `content.fetch` command for the submitted URL resolves it —
`final_url` fills `effective_url`, re-derives `domain_name`
(`ensure_domain_and_resolve_suspension`), first bytes seed media type and baseline in the
same apply. Terminal failure → ERROR health with reason, item editable for URL fix.
Dashboard shows the probing state; the create form returns immediately. This closes the
"Watcher fetches outside Replicator's politeness envelope" exception named in the
boundaries charter.

## Cutover (open question for review)

Recommended: **flagged dual-run.** `WATCHER_FETCH_MODE=local|bus` read at issue time —
`local` keeps today's inline fetch; `bus` issues commands. Soak on `bus`, watching:
facts correlate (no unknown `command_id`s), fingerprint continuity (UA pinned via command
headers should make revisions byte-identical — any drift shows as a `CHANGE_DETECTED`
burst), reaper re-issue rate ~0. Contingency, not default: a re-baseline pass suppressing
notifications on each item's first post-cutover revision. Flag removed after soak; the
`local` branch deleted, not preserved.

## Accepted gaps (explicit)

1. **429 adaptive backoff** — no signal path (429 is transient → no fact). Replicator's
   ~60s reclaim spacing is the interim; replicator#25 is the fix. Not a blocker.
2. **Non-terminal failure visibility** — a struggling origin looks like silence until
   terminal or timeout. Deferred with replicator#9 §3.
3. **`content.blobs` retention** — no floor is guaranteed; our consumer group's lag must
   stay near zero. The reaper covers a trimmed-away fact (it looks like a stall).
4. **Gate race (CR-5)** — the open-command gate is check-then-insert; a concurrent
   check-now + schedule-tick pair can both pass it and issue two commands. Contract-legal
   (two occasions), self-healing (the second apply supersedes), cost is one wasted origin
   fetch. If it shows up in practice, the fix is a partial unique index on
   `watched_item_id WHERE status IN (open)`.
5. **`health_status` enum gains `"probing"` (CR-6)** — additive on the OpenAPI surface;
   a strict client that compiled the old three-value enum will reject it. Watcher's only
   known consumer is its own dashboard.
6. **Failure-apply exhaustion (CR-13)** — if `apply_fetch_failure` exhausts its retries,
   the row is `failed` with no `applied_at` and the reaper's status filter skips it: the
   item misses that command's ERROR surfacing. Not a deadlock — `failed` isn't an open
   status, so the gate lifts and the next scheduled command repeats the failure with a
   fresh fact and a fresh apply. Delayed surfacing only; accepted.

## Testing

Fakeredis end-to-end in-suite: issue → hand-crafted `BlobAvailableEvent`/`FetchFailedEvent`
frames → consume → apply, pinning: per-occasion `command_id` (two issues, two ids);
duplicate-fact upsert idempotence; same-fingerprint two-command correlation (MUST-5);
out-of-order supersession; open-command scheduling gate; reaper re-issue lineage + cap;
publish-crash replay (`pending_publish` sweep, same `command_id`); OOM-retryable publish.
Live smoke after deploy against one sacrificial WatchedItem before the flag widens.

## Sequencing

1. Migration + `fetch_commands` model + issue path behind `WATCHER_FETCH_MODE` (default
   `local` — inert when merged).
2. Consumer task + apply/failure paths + reaper + gate (still inert).
3. Async create (`probing` state) — independently mergeable.
4. Flip one item to `bus`, soak, widen, delete `local`.
5. Cleanup: rate-limiter retirement, dashboard backoff UI, AGENTS.md single-process
   section (the limiter stops being the reason), epic close-out on #241.
