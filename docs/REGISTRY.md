# Registry reconciliation

How the `info.registry` announcements Archiver publishes become WatchedItems: what an announcement owns, what survives reconciliation, every 409 a reconciled item answers, and the linkage lifecycle. The entity itself is [WATCHED-ITEMS.md](WATCHED-ITEMS.md).

## Registry reconciliation (#254)

`info.registry` announcements are the authority on cadence and active state.
`src/workers/registry_reconcile.py` makes `watched_items` match them; the stream
mechanics (groupless tail, replay from `0-0`, no DLQ, `generation` ordering) are in
[BUS.md](BUS.md).

**What an announcement owns**, and nothing else: `archiver_info_source_id`,
`effective_url`, `source_specs`, `announced_schedule_config`, `is_active` — plus
`domain_name` and its two denormalized facts, and **only when the host actually
moves**. Re-deriving the domain on every announcement would clear a
`domain_suspended` an operator set, which is host-level mechanism the registry has
no opinion on.

**What an announcement records (#274).** Every applied announcement leaves an
audit row. A create emits `watched_item.created` with `source: "registry"` —
parity with the API create path, so a registry-born item does not read as having
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

No notification fires. `WatchEventType` is a closed enum persisted in
`notification_templates.events`; a member for this would drag in the template UI
and dispatch for a signal the audit row and the detail-page badge already carry.

**Spec acknowledgement (#274).** `last_reviewed_at` means *the operator has
acknowledged the current `source_specs`* — compared against the newest
`announcement_applied` event whose diff touched `source_specs`
(`unacknowledged_spec_change`, `src/dashboard/context.py`). `NULL` reads as
*never acknowledged*, not *acknowledged at the dawn of time*. Narrow on purpose:
a cadence re-announcement does **not** raise it, because
`announced_schedule_config` changes *when* the fingerprint is taken, not what it
means — a prompt firing for both would be trained away, taking the one that
matters with it.

**What survives reconciliation**: `health_status`, `last_checked_at`,
`last_observed_at`, `last_changed_at`, `last_reviewed_at`, `last_error_notified_at`,
`domain_suspended`, `archived_at`,
`throttle_floor_interval`, `default_schedule_config`, `content_media_type`,
`default_tags`, `description`, `name`, notification config, audit rows, fetch-command
history. Pinned by `TestLocalColumnsSurvive` — "we did not write it" is a weaker
guarantee than "a test fails if someone does".

**Three signals, and a fourth that is not a signal.** `revoked: true` deletes the
row (and records the generation in `revoked_info_items`, so a stale live
announcement arriving after the tombstone cannot resurrect it). `active: false`
keeps the row and stops scheduling — collapsing that into revoked loses the pause on
the next reconcile. `active: true` schedules. `active: null` is an **abstention**:
the registry has no opinion yet, so the column is left exactly as it is. Reading
`null` as `true` would un-pause every item an operator paused, which is precisely
what the rollout window looks like before CannObserv/archiver#150's import populates
the column.

**A local pause is not sticky — and since the 2026-08-13 cutover, not offered.**
`active` applies unconditionally, and once an item is reconciled
(`applied_generation` set) the API PATCH and the dashboard toggle both 409/flash
naming Archiver as the authority (`RegistryOwnedActivationError` in
`set_watched_item_active`) — a control that silently reverts within the snapshot
period is worse than a refusal that says where the control lives. Never-announced
rows keep the local toggle. Item-level pause lives in Archiver's dashboard alone.

**The guard covers all five owned columns, because the snapshot cannot repair local
drift.** The hourly republish carries the same generation, which the `>` ordering
guard ignores as stale — so a local write to an announcement-owned column diverges
until the next *real* registry mutation, not the next snapshot. Hence: PATCH 409s
`effective_url` / `source_specs` / `archiver_info_source_id` on reconciled items
(the dashboard URL edit flashes the same rule), and **restore clears `archived_at`
without re-activating** a reconciled item — archive→restore was otherwise a
two-step bypass of the pause guard. A restored registry-owned item stays paused
until Archiver re-arms it — one click, not a round-trip: Archiver's watch-active
route writes and announces unconditionally, so pressing resume there propagates
even when it already considers the item active. Watcher-local fields (name, description, tags, item
cadence, media type) stay editable everywhere. What remains legitimately Watcher's is
*mechanism* — local backoff, `domain_suspended` as the host-level break-glass, and
the throttle floor. `archived_at` is never touched, so an `active: true` against an
archived row reconciles the row's contents but no-ops on scheduling (`schedule_tick`
gates on `archived_at IS NULL` too) rather than resurrecting it.

**Two cadence absences, one answer.** `watch_spec` is required on a live
announcement since cannobserv#324, so delegation is spelled exactly one way:
`{"schema_version": 1}` with no `interval`, meaning *apply your own default* — for
this repo the per-domain tier. An `interval` that does not parse resolves the same
way and **must not stop scheduling**; co-core deliberately does not validate the
document's contents, because raising at decode on a no-DLQ stream would drop the
message and leave the key stale.

**The announced cadence does not live in `default_schedule_config`.** That column
has an operator writing to it, so reconciling into it would let the hourly snapshot
revert every operator edit — and it is what archiver#150 imports out of Watcher. The
`reduce_frequency` throttle moved to a floor for the same reason in the other
direction: as a tier it would be outranked by the announced cadence and silently
cleared on the next announcement.

**The floor is releasable, and only by an operator.** Writing an explicit item
cadence — `PATCH /api/v1/watched-items/{id}` with `default_schedule_config`, or the
dashboard's inline interval field — clears `throttle_floor_interval`. Both go through
`set_item_schedule_config` (`src/core/watched_items.py`), the single owner of that
write; `set_item_schedule_interval` is the string-shaped front door the dashboard
uses. Without that the escape hatch would be gone: before the
split, editing the interval *was* how a throttle was undone, and a floor nothing
clears means one temporal profile firing caps an item at 1d forever while the
operator's edits appear to do nothing. Reconciliation deliberately does not clear it;
the registry has no opinion on mechanism.

**A registry-owned WatchedItem cannot be deleted here.** `DELETE
/api/v1/watched-items/{id}` 409s once `applied_generation` is set, naming Archiver as
the authority: the stream is level-triggered, so the next announcement recreates the
row, and absence is not revocation — only a `revoked: true` tombstone retires a key.
Rows the registry has never announced still delete.

## Registry linkage and lifecycle

**Every WatchedItem is an Archiver InfoItem being watched (#251).**
`archiver_info_item_id` and `archiver_info_source_id` are both **NOT NULL** —
bare-URL WatchedItems were rolled back (epic: CannObserv/archiver#137 step 1).
Two create paths since #254. `POST /api/v1/watched-items` requires all four of
`archiver_info_item_id` + `url` + `archiver_info_source_id` + a **non-empty**
`source_specs` (both ids validated as canonical uppercase ULIDs at the boundary,
a constraint the OpenAPI document advertises; `source_specs` became required in
#260 — a spec-less item has no defined extraction, and Archiver never provisions
one, so PATCH holds the same non-empty floor); **no dashboard create**. The
`info.registry` reconcile is the second: it creates from an announcement alone,
so a cold start converges from the snapshot without anyone calling the API — and
it is **not** gated on `source_specs`, because an announcement is authoritative
for that column ([CONTENT-PIPELINE.md](CONTENT-PIPELINE.md) → *Extraction
outcomes* has the residual that leaves). The POST no longer validates the
InfoItem over HTTP — that was watcher's last outbound call and it went with the
SDK — which makes the endpoint redundant once archiver#141's producer is live. It
has had **no caller since archiver#158 (2026-08-17)**; it still works, and is kept
for manual provisioning and tests. The nullability had been paying for two
silent-drop branches on the SourceRevision path — both gone, so a captured
revision is always enqueued. Full detail, including why a fresh item starts
`unknown` rather than `probing`:
**[docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md)**. On any PATCH that sets
`effective_url` (the URL-succession path), `domain_name` is re-derived from the
URL **without** re-probing and `domain_suspended` is re-evaluated; every
create/PATCH/URL-change path (API and dashboard) shares
`ensure_domain_and_resolve_suspension` in
`src/core/domains.py` (#196). SourceRevisions are published to Archiver as
`source_revision_observed` facts on `content.revisions` (#253) on every detected
change, and again for the latest revision when a full fetch renews its blob
reference (#293); the local `pending_archiver_sync` outbox + drain worker
guarantees delivery during broker outages. Notifications dispatch inline from the pipeline **once per
WatchedItem** on change detection (`notifications_dispatched ≤ 1`), with
`change_revision_id` in WatchEvent metadata. `schedule_tick` skips items that
are paused (`is_active=false`), archived, or `domain_suspended`, and applies the
temporal profile's post-actions (deactivate / archive / reduce_frequency) to the
WatchedItem itself.
