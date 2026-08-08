# Content Pipeline

How a WatchedItem becomes bytes, a fingerprint, and a SourceRevision — and what
Watcher owns on each side of that path. Two boundaries meet here: Replicator
does the fetching, Archiver holds the registry, and Watcher issues commands to
one while projecting rows from the other. AGENTS.md carries the one-line
summaries and points here for the mechanics.

Two normative contracts live in the **Replicator** repo. Link, never copy:

- [`content-fetch-issuer-contract.md`](https://github.com/CannObserv/replicator/blob/main/docs/contracts/content-fetch-issuer-contract.md)
  — the seven MUSTs: per-occasion `command_id`, persist-before-publish, correlate
  on `command_id` only, idempotent upsert, no fingerprint dedupe, handle
  `fetch_failed` + keep a reaper, copy blob bytes before expiry. Note `blob_uri`
  is a host-local `file://` — VM-local by contract.
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
  `watcher` with a single member, started in the lifespan whenever
  `WATCHER_BUS_REDIS_URL` is set. Correlates on `command_id` only, never dedupes
  on fingerprint, branches `terminal` first on `fetch_failed`.
- **The apply tasks** — `apply_fetch_blob` / `apply_fetch_failure` in
  [`src/workers/fetch_commands.py`](../src/workers/fetch_commands.py):
  status-guarded against duplicates, supersession-guarded against out-of-order
  facts, blob-unreadable → re-issue. They own all check bookkeeping via the
  shared `_record_check_success` / `_record_check_failure` in
  [`src/workers/tasks.py`](../src/workers/tasks.py).
- **The reaper** — `reap_fetch_commands`, every 5 minutes, keyed on signal age
  `coalesce(fact_at, published_at)`. A stale row holding a blob fact gets its
  apply **re-deferred**; anything else is expired and re-issued with `intent_id`
  lineage. Knobs: `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS`,
  `WATCHER_FETCH_MAX_REISSUES`; hitting the cap sets ERROR health and lifts the
  gate.

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
failing command INSERTs. [`DEPLOYMENT.md`](DEPLOYMENT.md) → "No safe order" has
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

`Domain.current_interval` / `max_concurrency` / `decay_window` survive as **inert
columns** — nothing *reads* them for behavior, though creates still initialise
them and the API still accepts/echoes them; `last_request_at` has no writer at
all. They are off the dashboard (an editable knob that changes nothing is worse
than a hidden column). Dropping them is a separate migration that must also
remove the create/PATCH write sites and the `DomainResponse` fields.

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
order — restart first, then upgrade. See `docs/DEPLOYMENT.md` →
"Restart-before-migrate".
