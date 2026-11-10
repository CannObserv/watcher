# Registry reconciliation

How the `info.registry` announcements Archiver publishes become WatchedItems: what an announcement owns, what survives reconciliation, and the linkage lifecycle. Since #378 no operator surface in Watcher edits an item, so no local edit is refused either. The entity itself is [WATCHED-ITEMS.md](WATCHED-ITEMS.md).

## Registry reconciliation (#254)

`info.registry` announcements are the authority on cadence and active state. Since
F2 (#375) the announced interval is the only cadence Watcher reads — announced, else
the system default ([WATCHED-ITEMS.md](WATCHED-ITEMS.md) → *Fields and schedule
resolution*).
`src/workers/registry_reconcile.py` makes `watched_items` match them; the stream
mechanics (groupless tail, replay from `0-0`, no DLQ, `generation` ordering) are in
[BUS.md](BUS.md).

**What an announcement owns**, and nothing else: `archiver_info_source_id`,
`effective_url`, `source_specs`, `announced_schedule_config`, `is_active` — plus
`domain_name` and its two denormalized facts, and **only when the host actually
moves**. Re-deriving the domain on every announcement would clear a
`domain_suspended` an operator set, which is host-level mechanism the registry has
no opinion on — the `src.ops.suspend_domain` break-glass (#372) relies on it.

**What an announcement records (#274).** Every applied announcement leaves an
audit row. A create emits `watched_item.created` with `source: "registry"` —
parity with the API create path (deleted at #378), so a registry-born item does not read as having
appeared from nowhere. An update emits `watched_item.announcement_applied`
carrying `changes: {column: {old, new}}` over the five owned columns above,
**only when that diff is non-empty**: the hourly snapshot re-announces every item
unchanged, and an event per item per hour would bury the one that matters. The
generation guard returns before any write, so the groupless replay from `0-0` at
every boot records nothing (`test_a_boot_replay_records_nothing`).

`domain_name` is deliberately absent from the diff — derived, and it only ever
moves with `effective_url`, which is already there.

`source_specs` is compared through `canonical_specs` (`src/core/validators.py`),
the same canonicalisation `validator_source_key` hashes: order **significant**
across the list (the fallback loop tries specs in order, so a reorder can bind a
different spec), order **insensitive** within one spec's keys (a JSONB round-trip
does not preserve them). One notion of "did the specs change?" — two would be a
bug farm, and the conditional-GET invalidation (#269) already depended on this
one.

No notification fires: the audit row carries the signal. The operator's spec
acknowledgement (#274) went with the dashboard (#377), and #380 dropped its
`last_reviewed_at` column.

**What survives reconciliation**: `health_status`, `health_reason`,
`health_status_code`, `error_since`, `last_checked_at`,
`last_observed_at`, `last_changed_at`, `last_full_fetch_at`,
`domain_suspended`, `content_media_type`, the validator state (`etag`,
`last_modified`, `validator_source_key`), audit rows, fetch-command history.
Pinned by `TestLocalColumnsSurvive` — "we did not write it" is a weaker
guarantee than "a test fails if someone does". #380 dropped the rest of the old
list: `last_reviewed_at`, `archived_at`, `throttle_floor_interval`,
`default_schedule_config`, `default_tags`, `description`, `name` and the
notification templates.

**Three signals, and a fourth that is not a signal.** `revoked: true` deletes the
row (and records the generation in `revoked_info_items`, so a stale live
announcement arriving after the tombstone cannot resurrect it). `active: false`
keeps the row and stops scheduling — collapsing that into revoked loses the pause on
the next reconcile. `active: true` schedules. `active: null` is an **abstention**:
the registry has no opinion yet, so the column is left exactly as it is. Reading
`null` as `true` would un-pause every item an operator paused, which is precisely
what the rollout window looks like before CannObserv/archiver#150's import populates
the column.

**A local pause is not offered.** `active` applies unconditionally, and
item-level pause lives in Archiver's control plane alone. A control that
silently reverts within the snapshot period is worse than none, which is why the
API refused a toggle on a reconciled item until #378 deleted it.

**The snapshot cannot repair local drift.** The hourly republish carries the same
generation, which the `>` ordering guard ignores as stale — so a local write to an
announcement-owned column would diverge until the next *real* registry mutation,
not the next snapshot. Nothing in Watcher writes one since #378. Archiver's watch-active
route writes and announces unconditionally, so pressing resume there propagates
even when it already considers the item active. What remains Watcher's is
*mechanism*: change determination, health, media-type detection, and
`domain_suspended` as the host-level break-glass.

**Two cadence absences, one answer.** `watch_spec` is required on a live
announcement since cannobserv#324, so delegation is spelled exactly one way:
`{"schema_version": 1}` with no `interval`, meaning *apply your own default* — for
this repo the system default since F2 (#375); before it, the per-domain tier. An `interval` that does not parse resolves the same
way and **must not stop scheduling**; co-core deliberately does not validate the
document's contents, because raising at decode on a no-DLQ stream would drop the
message and leave the key stale.

*History:* the announced cadence got its own column rather than reusing the
operator-written `default_schedule_config`, and the `reduce_frequency` throttle
was a floor rather than a tier, so that neither writer could silently revert the
other. F2 (#375) stopped reading both, and #380 dropped them.

**A WatchedItem cannot be deleted here.** The stream is level-triggered, so the
next announcement would recreate the row, and absence is not revocation — only a
`revoked: true` tombstone retires a key. The API's `DELETE` (which 409'd on a
reconciled item) went at #378.

## Registry linkage and lifecycle

**Every WatchedItem is an Archiver InfoItem being watched (#251).**
`archiver_info_item_id` and `archiver_info_source_id` are both **NOT NULL**.
**One create path:** the `info.registry` reconcile (#378 deleted the uncalled
`POST /api/v1/watched-items`). It creates from an
announcement alone, so a cold start converges from the snapshot — and it is
**not** gated on `source_specs`, because an announcement is authoritative for that
column ([CONTENT-PIPELINE.md](CONTENT-PIPELINE.md) → *Extraction outcomes* has the
residual that leaves). The nullability's history, why a fresh item starts
`unknown`, and the URL-succession path:
[CONTENT-PIPELINE.md](CONTENT-PIPELINE.md) → *Registry linkage (#251)*. SourceRevisions are published to Archiver as
`source_revision_observed` facts on `content.revisions` (#253) on every detected
change, and again for the latest revision when a full fetch renews its blob
reference (#293); the local `pending_archiver_sync` outbox + drain worker
guarantees delivery during broker outages. A detected change also stages one
`change_detected` fact on `info.watch-events` (#369), which Archiver notifies
from; Watcher dispatches nothing itself since F3 (#376). `schedule_tick` skips items that
are paused (`is_active=false`) or `domain_suspended`. Archiver runs the temporal
profiles' post-actions (CannObserv/archiver#365); Watcher applied them until F2 (#375).
