# Content revisions

The producer half of the content pipeline: how a detected change becomes a
`source_revision_observed` frame on `content.revisions`, and how an unchanged
fingerprint re-announces a renewed blob reference. Split out of
[CONTENT-PIPELINE.md](CONTENT-PIPELINE.md), which covers fetch → extract →
fingerprint and the change decision that enqueues the rows described here.

## Reporting revisions on `content.revisions` (#253)

`SourceRevisionObservedEvent` carries values the outbox row never held, so
`pending_archiver_sync` gained six columns, written at enqueue time by
`process_watched_item`:

| Column | Source |
|---|---|
| `command_id`, `blob_uri`, `blob_expires_at` | the correlated `content.blobs` fact, via `BlobProvenance` |
| `source_media_type` | that fact's normalized `media_type` — what the origin served |
| `content_media_type` | the **extracted** content's type (`text/plain; charset=utf-8`) — a different thing, which is why the wire keeps both |
| `spec_fingerprint` | co-core's derivation over the spec the fallback loop actually bound |

`fetch_commands.blob_expires_at` was added to feed the first row: the fact has
carried it since cannobserv#301 and the consumer was dropping it. It is echoed
onward under the same name, **never** derived from the issuer contract's MUST-7
TTL — that is Replicator's policy, on a clock that runs from last fetch
reference, an event no consumer observes. NULL means the horizon is unknown, and
Archiver records absence rather than a guess.

Snapshotted rather than joined from `fetch_commands` at drain time: the command
row's lifecycle is not the outbox row's — delivery to Archiver is the thing being
guaranteed — and the apply path already holds the values. `command_id` therefore
carries no FK.

`drain_pending_archiver_sync` then publishes each row as
`source_revision_observed` — it no longer POSTs. **The outbox stays**: it is the
producer-side durability guarantee, and only the transport moved. Watcher emits
an *observation* and Archiver decides what to persist; no `source_revision_id`
travels, because a service that does not own the registry mints no registry ids.
Redelivery is safe by construction — the envelope key
`info_source_id:extracted_fingerprint` matches Archiver's uniqueness constraint,
so an at-least-once repeat is an idempotent no-op there.

**Two failure classes, and conflating them is the bug the drain is shaped to
avoid.** Building the payload is pure, so a failure is *deterministic* —
identical every loop — and the row is stamped `dead_lettered_at` at once rather
than spinning forever. Publishing can fail because the broker is down, full
(#288) or denying the command under its ACL (#290), all of which are
*transient*: retry indefinitely, exempt from the ceiling, because an outage is
not the row's fault and a data-loss cliff at attempt N discards real revisions.
Mirrors Archiver's own producer split. The classifier's membership and the two
`ResponseError` subclasses that do not look like outages:
[BUS-CONNECTION-POLICY.md](BUS-CONNECTION-POLICY.md). What classification does
*not* change is the backoff — `mark_failure` runs on both branches, so a row's
own interval is the same either way. What shortens it is the **next successful
publish**: `clear_backoffs` pulls every failure-delayed row forward on any pass
that publishes something, because one accepted `XADD` is the evidence about the
broker that the waiting rows are missing (#291). Recovery after an outage is the
next tick, not the 3600 s cap.

That replaced an `attempts < 10` filter in `select_due` which was neither: it
silently stopped selecting a row without marking it, so an outage lasting ten
backoffs abandoned revisions with no signal and nothing to find them by.
`docs/DEPLOYMENT.md` carries the query for dead-lettered rows — a flat backlog
count no longer tells the whole story.

**Retired with the transport:** the scratch cache (`src/core/sources/scratch.py`),
its sweeper, the `WATCHER_CACHE_*` variables, and the back-population of
`ChangeRevision.archiver_revision_id`. Watcher was writing its own copy of bytes
Replicator had already stored, reporting *that* path as `content_cache_uri`,
sweeping it, then PATCHing null — three moving parts doing nothing `blob_uri`
does. `archiver_revision_id` existed only so the sweeper could PATCH against it;
it is gone from the API response too (a deliberate breaking change over shipping
a permanently-null field).

All three columns are now **dropped**, in `f4a8b26c9d31` (#261). The two cache
columns were an expand/contract: no single deploy order makes dropping a NOT
NULL column safe, so `32140463c26c` released them to nullable and the contract
waited until the publisher was live. `archiver_revision_id` was different in
kind — dead but holding real ids — and dropping it costs nothing, because
Archiver identifies a SourceRevision by `(info_source_id, content_fingerprint)`
(`uq_source_revisions_source_fingerprint`, the pair its upsert conflicts on).
The local copy was redundant, not unique; the mapping is re-derivable from the
fingerprint Watcher still stores.

`spec_fingerprint` is **per-spec** (cannobserv#309), so a fallback from `spec[0]`
to `spec[1]` moves it; Archiver reads the position that implies as a selector-rot
signal (archiver#139), and its policy is record-and-flag, never reject. It
reports `None` for a spec co-core cannot derive from (it rejects floats, explicit
nulls, non-ASCII keys), because a diagnostic must never cost a revision. The
other `None` case — an item with no `source_specs` at all — no longer produces a
revision to attribute: #260 made that item unextractable rather than a full-page
watch under a spec present in no registry.

## A renewed blob reference is re-announced (#293)

Replicator re-references the blob on every full re-fetch of unchanged bytes —
the store short-circuits, the object's `customTime` is refreshed, and a fresh
`blob_available` goes out with a later `blob_expires_at` (replicator
`docs/STORAGE.md`). The validator age ceiling
([CONDITIONAL-GET.md](CONDITIONAL-GET.md)) forces such a re-fetch at least
weekly even for an origin answering 304. So the blob was renewed weekly
cluster-wide while Archiver's `content_cache_expires_at` for the pair stayed
at the first observation, and its replication issuance refused every occasion
once that horizon passed. archiver#201 is the consumer half: a re-observation
refreshes the two cache columns together, forward-only, and emits nothing.

The producer half is the cache-hit branch of `process_watched_item`. An
unchanged fingerprint still writes no `ChangeRevision` and dispatches nothing,
but it **upserts** a `PendingArchiverSync` for the item's latest revision
carrying this cycle's provenance — the same six columns the change path
writes (`_provenance_columns`), so the drain publishes the same shape under
the same envelope key. `change_revision_id` is unique on the outbox, and the
upsert is one statement so it resolves in Postgres against a drain holding
the row `FOR UPDATE`:

- a row the drain has not published yet takes the newer provenance in place
  and is pulled to *now* — one row, the freshest reference;
- a dead-lettered row is revived (`dead_lettered_at` / `last_error` cleared,
  `attempts` kept): the verdict was about values the renewal has replaced.

**A renewal may only ever improve a row.** It is the one writer that overwrites
provenance rather than creating it, so a reference missing a wire-required
field (`blob_uri`, `source_media_type`) would replace a publishable row with
one the drain dead-letters — a real revision lost to a refresh, where the
change path's equivalent gap costs only an observation that never existed. The
renewal declines and logs instead, and reports `renewal_enqueued=False`.
Unreachable today (`aread_blob` raises before the pipeline on a null URI, and
the consumer writes `media_type` in the same upsert); the guard is there
because the asymmetry is not otherwise enforced.

**The baseline is never renewed.** The first revision is the one the change
path never enqueued, so re-announcing it would be Archiver's *first*
observation of the pair — a registry insert and a `source_revision_captured`
on `info.changes`, not a horizon refresh. The rule is "the latest revision has
an older sibling", the same test the dashboard uses to keep baselines out of
`changes_today`. That a stable item's baseline never reaches Archiver at all
is a separate gap, not closed here.

Traffic is bounded by full fetches, each of which already produced a
`content.blobs` fact; a 304 touches no blob and correctly announces nothing.
The apply result carries `renewal_enqueued` beside `changed` /
`baseline_established`, the `CHECK_NO_CHANGE` audit carries the same key when
it fired (present-or-absent, never a `False` — the same shape the 304 path uses
for `source`), and the pipeline logs each renewal at INFO. A declined renewal
logs at WARNING and sets none of them.
