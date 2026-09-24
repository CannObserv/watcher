# Content Pipeline

How a WatchedItem becomes bytes, a fingerprint, and a SourceRevision — and what
Watcher owns on each side of that path. Two boundaries meet here: Replicator
does the fetching, Archiver holds the registry, and Watcher issues commands to
one while projecting rows from the other. AGENTS.md carries the one-line
summaries and points here for the mechanics.

Two normative contracts live in the **Replicator** repo. Link, never copy:

- [`content-fetch-issuer-contract.md`](https://github.com/CannObserv/replicator/blob/main/docs/contracts/content-fetch-issuer-contract.md)
  — the eight MUSTs: per-occasion `command_id`, persist-before-publish, correlate
  on `command_id` only, idempotent upsert, no fingerprint dedupe, handle
  `fetch_failed` + keep a reaper, copy blob bytes before expiry, handle
  `not_modified` before sending a validator. `blob_uri` is `file://` on
  Replicator's host under the default backend and `gs://co-gcs-blobs/…` under
  the object-store backend production runs (replicator#7) — branch on the
  scheme, parse nothing below it.
- [`replicator-boundaries.md`](https://github.com/CannObserv/replicator/blob/main/docs/contracts/replicator-boundaries.md)
  — what belongs on which side of the fetch boundary.

## Phase 4 (#241) — Watcher is the issuer, not the fetcher

**Done.** Watcher **is** the `content.fetch` issuer and `content.blobs` consumer;
it makes no origin request of its own on any scheduled path. Cut over
2026-08-06; `WATCHER_FETCH_MODE` and the inline-fetch branch were deleted in
step 5 (soak record + retirement notes: the design doc's cutover section).
Design: [`docs/plans/2026-08-06-phase-4-content-fetch-producer-design.md`](plans/2026-08-06-phase-4-content-fetch-producer-design.md).

What that leaves in the code:

- **The `fetch_commands` table** — outbox + pending map + inbox, keyed
  `command_id` ([`src/core/fetch_commands.py`](../src/core/fetch_commands.py)).
- **The issue path** — the whole of `check_watched_item` now: fresh ULID per
  occasion, persist-before-publish plus an every-minute sweep, pinned watcher
  User-Agent, `info_source_id`, one-open-command gate.
- **The `content.blobs` consumer** —
  [`src/workers/fetch_facts.py`](../src/workers/fetch_facts.py), consumer group
  `watcher.blobs` with a single member — derived by `group_name`, never
  hand-written (#285) — started in the lifespan when `WATCHER_BUS_REDIS_URL` is
  set **and** `WATCHER_BUS_ENABLED=1` (#262: a URL is configuration, not
  permission). Correlates on `command_id` only, never dedupes
  on fingerprint, branches `terminal` first on `fetch_failed`.
- **The apply tasks** — `apply_fetch_blob` / `apply_fetch_failure` /
  `apply_fetch_not_modified` in
  [`src/workers/fetch_commands.py`](../src/workers/fetch_commands.py):
  status-guarded against duplicates, supersession-guarded against out-of-order
  facts, blob-unreadable → **capped** re-issue (#275, below). They own all check
  bookkeeping via the shared `_record_check_success` / `_record_check_failure`,
  which live in that same module.
- **The reaper** — `reap_fetch_commands`, every 5 minutes, keyed on signal age
  `coalesce(fact_at, published_at)`. A stale row holding a blob fact gets its
  apply **re-deferred**; anything else is expired and re-issued with `intent_id`
  lineage. Knobs: `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS`,
  `WATCHER_FETCH_MAX_REISSUES` (shared with the apply path — it caps a lineage,
  not a sweep); hitting the cap sets ERROR health and lifts the gate.

### An unreadable blob is capped, not retried forever (#275)

Reading `blob_uri` is the one place Watcher parses that URI, so the scheme
dispatch lives in [`src/core/blobs.py`](../src/core/blobs.py), not in the
worker: a backend registers a spooler there. Non-local backends stream onto a
temp file `blob_file` removes on the way out; a `file://` blob is Replicator's
own and is yielded in place. Async callers await `aread_blob` — the apply task
shares its process with the API. The `gs://` arm (CannObserv/replicator#7)
reads `gs://co-gcs-blobs` as the `GCS_BLOB_CREDENTIALS` identity, key used
verbatim — flat, never the `file://` shard rule. The two error types are the
decision:

- **`BlobUnreadable`** — backend understood, blob missing: reaped between fact
  and apply, or a `gs` 404 (the lifecycle rule ran — `blob_expires_at` is a
  floor, not a promise). Re-issue, **capped** at
  `WATCHER_FETCH_MAX_REISSUES` against the same `reissue_count` the reaper
  reads. The cap is load-bearing because the re-issue publishes immediately: the
  scheduling gate never sees it, so an uncapped loop runs at Replicator's fetch
  round-trip rather than the item's interval, each turn a real origin request.
  Systematic causes today: a permissions or mount change under Replicator's
  blob dir, or a blob dir moved without both services updated.
- **`UnsupportedBlobScheme`** — re-fetching buys nothing: an unknown scheme, a
  `gs` 401/403 (the grant is an operator's to fix), an unset credential.
  Terminal on the first occasion, zero re-issues.

Both end `FAILED` with `failure_reason="blob_unreadable"` (distinct from
`fetch_timeout` — the remedy is the blob store, not the origin),
`CHECK_FETCH_FAILED`, ERROR health, one `WATCH_ERROR` on the transition, and the
gate lifts so the item re-enters normal scheduling. Neither `stamp_full_fetch`
nor `clear_validators` fires: no bytes arrived, and being unable to read a blob
says nothing about the stored pair.

### `not_modified` is a success, not a failure (#249)

A 304 rides `FetchFailedEvent` with `reason="not_modified"`, `terminal=True`,
`status_code=304` — co-core's registry calls it "the one token on this event that
is **not** a failure", and rejected a dedicated `content_unchanged` event because
the event's real meaning is *"this command will not produce a blob"*. There is no
new dispatch arm; the consumer branches `terminal` first, then the reason.

Watcher's handling, top to bottom:

| Piece | Behaviour |
|---|---|
| Row status | `FetchCommandStatus.NOT_MODIFIED` — its own member, so a 304 is not confusable with `SUCCEEDED` ("a blob went through the pipeline") in any status-keyed query. No migration: `status` is a plain `String(20)`. |
| `failure_reason` | Stays NULL. At steady state this token outnumbers every real failure combined, so journalling it as one would destroy `failure_reason` as a signal. |
| Fingerprint | Nothing written. There is no item-level fingerprint to reuse (Watcher's extracted-text identity lives on `ChangeRevision`), and `fetch_commands.content_fingerprint` is Replicator's *raw-bytes* identity for an occasion that produced bytes. The item keeps the content it already has. |
| Apply | `apply_fetch_not_modified` → OK health, fresh `last_checked_at`, `last_observed_at` stamped (the content *was* verified current), `CHECK_NO_CHANGE` audit carrying `source: not_modified`, `WATCH_RECOVERED` if the item was in ERROR. Never `CHECK_FETCH_FAILED`, never `WATCH_ERROR`. |
| Revision half | Skipped entirely — no extraction, no `ChangeRevision`, no `PendingArchiverSync`, no `content.revisions` frame. |
| Gate / reaper | `OPEN_STATUSES` is a *positive* enumeration, so the new member is closed to the scheduling gate and invisible to the reaper for free. |

### Conditional GET: storing and replaying validators (#269)

Split out to [CONDITIONAL-GET.md](CONDITIONAL-GET.md) — the gate
(`WATCHER_CONDITIONAL_GET_ENABLED`), snapshot-at-issue, the deterministic
invalidation rules (`validator_source_key`, the age ceiling), and the
`invalid_request_options` clear.

### Extraction outcomes: empty is a failure (#258, #260)

`source_specs` are tried in order and the first yielding non-empty chunks wins.

**A spec-less item is unextractable, not a whole-page watch (#260).** The
synthetic `[{}]` full-page default — inherited unremarked from #185's pipeline
rewrite, never ratified — is gone, and with it the "optional at create"
affordance: `WatchedItemCreate.source_specs` is required and non-empty, `PATCH`
holds the same floor, and `process_watched_item` raises `ExtractionError` before
dispatching an extractor when a row carries none. Settled that way because
Archiver, the only caller, always has specs in hand: its registry refuses to
announce a source as live without non-empty `source_specs`, and provisioning
always sends them. Production carried 0 spec-less items of 4.

**The residual, stated rather than gated.** The `info.registry` reconcile writes
`list(payload.source_specs or [])` and co-core's announcement still declares the
field optional, so a spec-less row remains *reachable over the wire* after the
API door closed. That path is deliberately **not** gated a second time: an
announcement is authoritative for `source_specs`, and refusing one would break
the cold-start convergence #254 exists to provide. Such a row can only come from
a source Archiver would not announce as live, which therefore never schedules —
and if one ever does check, the pipeline guard is exactly what makes it loud
(ERROR health, no revision) instead of silent.

When **every** spec yields empty, `process_watched_item` raises `ExtractionError`
and writes nothing — no `ChangeRevision`, no `PendingArchiverSync`, no notification. It
lands on the same path a raising extractor takes: `CHECK_EXTRACTION_FAILED` +
ERROR health, dispatched once on the OK→ERROR transition.

Unconditional, on both sides of a baseline. The rule exists because empty
content fingerprints *consistently*: without it, selector rot presented as a
**content change** — a zero-byte revision POSTed to Archiver, a
`CHANGE_DETECTED` notification, health still OK — and an item broken from its
first check baselined on the empty digest and never reported again. A false
ERROR on a legitimately-emptied source is recoverable at an operator's glance; a
false "content changed" is silent. The guard is in `process_watched_item`, not
`_extract_and_fingerprint` — the extractor reports what it found, the caller
judges it.

### The fingerprint's bytes are co-core's (#324)

Step 0 of the Observo-derived extraction design
([design doc](plans/2026-09-24-observo-extraction-and-diff-design.md)).
`content_fingerprint` is `co_core.pure.extract.canonical_text_fingerprint` over
the chunks — byte-identical to the local join it replaced, so every stored
fingerprint is already the address derived text will be stored under; co-core's
per-extractor goldens trip on a fingerprint-moving change.

`ChangeRevision` gained two **nullable** columns (`ccc7de7cabf8`):
`spec_fingerprint` (previously discarded with the outbox row) and
`processor_version` (`EXTRACTION_GENERATION`, spelled through co-core's
`processor_version()` so a revision from a `content.derived` fact compares
alike). NULL means *unknown* — pre-migration rows, or a spec co-core cannot
derive from — and the design's Option A treats unknown as neither a spec label
nor a re-baseline. Nothing reads either column yet.

`src/core/media_type.py` re-exports `co_core.pure.extract.media_type`; its test
pins *identity*, because the issuer resolves the dispatch essence onto the
`content.process` command and the processor may re-resolve it. One behaviour
change: `spec_schema_version` replaces `int(...)`, so a boolean `schema_version`
raises `ExtractionError` where it read as `1`.

### Reporting revisions on `content.revisions` (#253, #293)

Split out to [CONTENT-REVISIONS.md](CONTENT-REVISIONS.md) — the six provenance
columns on `pending_archiver_sync`, the drain's two failure classes, what
retired with the scratch-cache transport, and the #293 renewal that re-announces
an unchanged fingerprint's blob reference.


### `info_source_id` on the wire (#252)

co-core **0.8.0** (cannobserv#300) makes `info_source_id` required on all three
content contracts and `BlobAvailableEvent.command_id` non-optional. On this side:

- **Issue.** `create_fetch_command` snapshots
  `WatchedItem.archiver_info_source_id` onto the `fetch_commands` row (`NOT
  NULL`) and `publish_fetch_command` sends it. Snapshotted rather than joined
  because the pending-publish sweep holds only the row — a join would also lose
  the issue-time value on a later InfoSource change.
- **Correlate.** Unchanged: `command_id` only (MUST-3). Facts still upsert onto
  the row; the echo is cross-checked against the command's own value and a
  mismatch logs a warning, never refuses the fact.
- **Discard.** An unmatched fact stays discarded. The stream is broadcast, so a
  fact naming one of our InfoSources may answer another issuer's command —
  fetched under a User-Agent watcher's fingerprints are sensitive to, which is
  why applying it would manufacture a change signal. `_log_orphan` reports it at
  WARNING with the WatchedItem the id resolves to. Recovery is deliberately not
  built; revisit only if production shows a nonzero orphan count.
- **MUST-2 is bookkeeping now.** The wire carries the domain key, so a lost row
  no longer makes a fact uncorrelatable in principle. Persist-before-publish
  stays — the row holds request options, health, re-issue lineage, and reaper
  state, none of which the wire replaces.

**Deploy ordering.** Replicator must ship its echo (replicator#28) to production
**before** watcher upgrades. A 0.8.0 consumer against 0.7.7 facts fails required-
field validation, and an undecodable frame is acked past — silent loss until the
reaper re-issues. The reverse ordering is safe (`extra="ignore"` on both sides).
Facts published before Replicator's upgrade and still unread at watcher restart
hit the same path, so prefer a quiet window.

The **migration** has its own ordering problem, unrelated to Replicator: no
order of `alembic upgrade head` and `systemctl restart` avoids a brief window of
failing command INSERTs. [`MIGRATIONS.md`](MIGRATIONS.md) → "No safe order" has
the procedure and what it looks like in the journal.

**Async create (step 3).** Nothing on a create path probes
(`resolve_watch_target`, [`src/core/watched_items.py`](../src/core/watched_items.py)).
Since #251 that helper's only caller is the dashboard's `effective_url` edit,
which re-enters `health_status='probing'` with the submitted URL as
`effective_url`; the next fact resolves it (`final_url` → `effective_url` +
domain re-derivation via the #196 helper, PROBING → OK/ERROR). Steady-state
redirects — and every Archiver-provisioned create, which starts `unknown` —
stay audit-only (`CHECK_REDIRECT_OBSERVED`).

**Retired in step 5, with the fetch path:** the in-process `DomainRateLimiter`
and its config poller + startup hydration, the 429 backoff/decay helpers,
`HttpFetcher` and the registry's fetcher slot, the create-time probe on
watched-item routes, and the dashboard Backoff badge/filter.

`Domain.current_interval` / `max_concurrency` / `decay_window` /
`last_request_at` survived step 5 as inert columns and were dropped in #272
(migration `10783d8a2405`, restart-before-migrate — see
[docs/MIGRATIONS.md](MIGRATIONS.md)) together with the API create/PATCH write
sites and the `DomainResponse` fields. An old client still sending the retired
request knobs gets the repo-wide unknown-field treatment: silently ignored,
not a 422.

#245 was the cutover's ordering blocker — politeness must not lapse when the
fetch path becomes a publish path — and shipped first.

## Registry linkage (#251) — every WatchedItem is an InfoItem being watched

`archiver_info_item_id` and `archiver_info_source_id` are both **NOT NULL**;
bare-URL WatchedItems were rolled back (epic: CannObserv/archiver#137 step 1;
production had zero bare rows). One create path remains, `POST
/api/v1/watched-items`, requiring `archiver_info_item_id` + `url` +
`archiver_info_source_id` — the Archiver "Begin Watching" provisioning call.
Both ids are validated as ULIDs at the boundary (`ULIDRefStr`,
[`src/api/schemas/types.py`](../src/api/schemas/types.py)), so a malformed
reference is a 422 rather than a row that fails later against a real captured
revision. **Canonical uppercase Crockford base32 only** — the same standard
`parse_ulid` holds path parameters to, since it is the same parser
(`ULID.from_str`, which rejects the lowercase form). The OpenAPI document
carries the matching `format: ulid` + `pattern`, so a generated client sees the
constraint rather than a bare string; a schema test pins the pattern and the
parser to the same accept-set. Archiver's provisioning call satisfies this by
construction — it sends `str()` of a `ULID`. There is **no dashboard create** (`/watched-items/new`, its form
template, and the "New Watched Item" CTA are gone); the list's empty state
points at Archiver.

What the nullability had been buying was two silent-drop branches on the
SourceRevision path — the pipeline's `if watched_item.archiver_info_source_id:`
gate around the scratch write + outbox insert, and the drain's matching guard —
both deleted, so a captured revision is now always enqueued and posted. The
drain keeps only its `wi is None` half (a WatchedItem deleted mid-batch,
reachable only across concurrent transactions since the pending row is
`ON DELETE CASCADE`).

`effective_url` is stored verbatim from the create call and `domain_name`
derived from it — no probe on any create path (#241), and a fresh item starts
`health_status='unknown'`, not `probing`: Archiver is authoritative for the URL.
On any PATCH that sets `effective_url` (the URL-succession path), `domain_name`
is re-derived from the URL **without** re-probing and `domain_suspended` is
re-evaluated; every create/PATCH/re-probe path (API and dashboard) shares
`ensure_domain_and_resolve_suspension` in
[`src/core/domains.py`](../src/core/domains.py) (#196).

**Deploy note.** `d5a71c93e0f2` is the one migration that inverts the standard
order — restart first, then upgrade. See `docs/MIGRATIONS.md` →
"Restart-before-migrate".
