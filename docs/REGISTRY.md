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

No notification fires: the audit row and the detail-page badge carry the
signal.

**Spec acknowledgement (#274) — retired.** `last_reviewed_at` meant *the operator has
acknowledged the current `source_specs`* — compared against the newest
`announcement_applied` event whose diff touched `source_specs`
(the dashboard's `unacknowledged_spec_change` until #377 deleted it; #380 dropped
the column). `NULL` read as
*never acknowledged*, not *acknowledged at the dawn of time*. Narrow on purpose:
a cadence re-announcement does **not** raise it, because
`announced_schedule_config` changes *when* the fingerprint is taken, not what it
means — a prompt firing for both would be trained away, taking the one that
matters with it.

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

**A local pause is not offered.** `active` applies unconditionally. From the
2026-08-13 cutover until #378 the API PATCH 409'd a toggle on a reconciled item
(`applied_generation` set), naming Archiver as the authority — a control that
silently reverts within the snapshot period is worse than a refusal that says
where the control lives. Since #378 Watcher has no operator surface that edits an item.
Item-level pause lives in Archiver's dashboard alone.

**The snapshot cannot repair local drift.** The hourly republish carries the same
generation, which the `>` ordering guard ignores as stale — so a local write to an
announcement-owned column would diverge until the next *real* registry mutation,
not the next snapshot. Until #378 the API refused one on every owned column of a
reconciled item, and its restore cleared `archived_at` without re-activating;
since #378 there is no local operator writer and no restore. Archiver's watch-active
route writes and announces unconditionally, so pressing resume there propagates
even when it already considers the item active. The Watcher-local fields (name,
description, tags, item cadence, the media-type override) lost their last
operator editor with the API (#378), and #380 dropped their columns. What remains legitimately Watcher's is
*mechanism* — local backoff and `domain_suspended` as the host-level break-glass
(the throttle floor was one until F2, #375). Until #380 an archived row no-op'd an
`active: true` on scheduling; nothing archives an item now, and the column is gone.

**Two cadence absences, one answer.** `watch_spec` is required on a live
announcement since cannobserv#324, so delegation is spelled exactly one way:
`{"schema_version": 1}` with no `interval`, meaning *apply your own default* — for
this repo the system default since F2 (#375); before it, the per-domain tier. An `interval` that does not parse resolves the same
way and **must not stop scheduling**; co-core deliberately does not validate the
document's contents, because raising at decode on a no-DLQ stream would drop the
message and leave the key stale.

**The announced cadence never lived in `default_schedule_config`.** That column
had an operator writing to it, so reconciling into it would have let the hourly
snapshot revert every operator edit — and it was what archiver#150 and the #373
export imported out of Watcher. The `reduce_frequency` throttle moved to a floor for
the same reason in the other direction: as a tier it would have been outranked by
the announced cadence and silently cleared on the next announcement. Since F2
(#375) neither column was read, and #380 dropped both.

**The floor was releasable, and only by an operator (inert since F2, #375).** Writing an explicit item
cadence — the API's item PATCH until #378, the dashboard's inline interval field
until #377 — cleared `throttle_floor_interval` through one writer,
`set_item_schedule_config`. Without that the escape hatch would be gone: before the
split, editing the interval *was* how a throttle was undone, and a floor nothing
clears means one temporal profile firing caps an item at 1d forever while the
operator's edits appear to do nothing. Reconciliation deliberately does not clear it;
the registry has no opinion on mechanism. Since F2 nothing writes the floor and the
resolver ignores it; the writer that cleared it went with `/api/v1` (#378).

**A WatchedItem cannot be deleted here.** The stream is level-triggered, so the
next announcement would recreate the row, and absence is not revocation — only a
`revoked: true` tombstone retires a key. The API's `DELETE` (which 409'd on a
reconciled item) went at #378.

## Registry linkage and lifecycle

**Every WatchedItem is an Archiver InfoItem being watched (#251).**
`archiver_info_item_id` and `archiver_info_source_id` are both **NOT NULL** —
bare-URL WatchedItems were rolled back (epic: CannObserv/archiver#137 step 1).
**One create path since #378:** the `info.registry` reconcile. It creates from an
announcement alone, so a cold start converges from the snapshot — and it is
**not** gated on `source_specs`, because an announcement is authoritative for that
column ([CONTENT-PIPELINE.md](CONTENT-PIPELINE.md) → *Extraction outcomes* has the
residual that leaves). From #254 to #378 `POST /api/v1/watched-items` was a second
path, with **no caller after archiver#158 (2026-08-17)**; #378 deleted it with the
rest of `/api/v1`. The nullability had been paying for two
silent-drop branches on the SourceRevision path — both gone, so a captured
revision is always enqueued. Full detail, including why a fresh item starts
`unknown` rather than `probing`:
**[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)**. When an announcement
moves `effective_url` (the URL-succession path), `domain_name` is re-derived from the
URL **without** re-probing and `domain_suspended` is re-evaluated through
`ensure_domain_and_resolve_suspension` in
`src/core/domains.py` (#196) — the reconcile is its one caller since #380 removed
the PROBING branch. SourceRevisions are published to Archiver as
`source_revision_observed` facts on `content.revisions` (#253) on every detected
change, and again for the latest revision when a full fetch renews its blob
reference (#293); the local `pending_archiver_sync` outbox + drain worker
guarantees delivery during broker outages. A detected change also stages one
`change_detected` fact on `info.watch-events` (#369), which Archiver notifies
from; Watcher dispatches nothing itself since F3 (#376). `schedule_tick` skips items that
are paused (`is_active=false`), archived, or `domain_suspended`. Until F2 (#375)
it also applied the temporal profile's post-actions (deactivate / archive /
reduce_frequency) to the WatchedItem itself; Archiver runs them now
(CannObserv/archiver#365).
