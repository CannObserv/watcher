"""Per-WatchedItem history: compare an outcome, record it, dispatch the change.

The processor extracts and fingerprints (#326, #350); this module holds what
happens to its answer. ChangeRevision rows serve as the local fingerprint
history; the first row is a baseline (no notification); subsequent changes
dispatch CHANGE_DETECTED once for the WatchedItem (the single monitored
entity, #191).
"""

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

from co_core.pure.extract import CANONICAL_TEXT_MEDIA_TYPE
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.utils import format_utc_iso, watched_item_event_base_metadata

logger = get_logger(__name__)

# The provenance fields ``SourceRevisionObservedEmit`` requires — the ones a
# renewal must not blank out on a queued row (#293, CR 1). Named here rather
# than derived from the model: the wire coupling belongs to the drain (#253),
# and importing the bus contract into the pipeline to answer this would move it.
# `tests/test_renewal_wire_contract.py` pins the pair against co-core's own
# model, so promoting a field to required there fails the suite rather than
# silently reopening the vector (CR 9).
WIRE_REQUIRED_PROVENANCE_FIELDS = ("blob_uri", "source_media_type", "content_media_type")


@dataclass(frozen=True)
class BlobProvenance:
    """The correlated ``content.blobs`` fact, carried onto the outbox row (#253).

    Supplied by the apply path, which holds the ``FetchCommand`` when it calls
    the pipeline. Required since the cutover: an observation Watcher cannot say
    where it came from has nothing to publish.

    Every field is nullable because its source column is (``fetch_commands`` fact
    fields are all populated by the consumer, so they are NULL until the fact
    lands). In practice a row that reaches apply has read its blob, so
    ``blob_uri`` is set, and ``media_type`` is required on ``BlobAvailableEvent``
    — but a dataclass validates nothing, and declaring ``str`` while a ``None``
    flows through would move the failure from the publisher's dead-letter path,
    where it is classified, to a type annotation nobody enforces (CR-2).
    """

    command_id: str
    blob_uri: str | None
    source_media_type: str | None
    blob_expires_at: datetime | None = None
    # Replicator's raw-bytes digest, the fact's ``content_fingerprint`` verbatim
    # (#329) — never parsed out of ``blob_uri``, never the extracted fingerprint.
    blob_fingerprint: str | None = None


@dataclass
class ExtractionOutcome:
    """What a ``content.derived`` fact reports about one occasion's text."""

    content_fingerprint: str
    content_size_bytes: int
    schema_version: int
    # Identity of the spec the chain actually bound — per-spec, so a fallback
    # from spec[0] to spec[1] moves it (cannobserv#309). ``None`` when the
    # processor could not derive one; a diagnostic must never fail a check.
    spec_fingerprint: str | None = None
    # The media type of the EXTRACTED text, not of what the origin served; the
    # wire keeps it beside ``source_media_type`` because they differ for one
    # revision. co-core's constant (#324).
    content_media_type: str = CANONICAL_TEXT_MEDIA_TYPE
    # Identity of the extraction itself (#324), as the processor reports it;
    # ``None`` is unknown, and Option A then triggers on nothing.
    processor_version: str | None = None


def _provenance_columns(blob: BlobProvenance, outcome: ExtractionOutcome) -> dict[str, object]:
    """The outbox columns that say where one observation came from (#253).

    One spelling for both writers. The change branch inserts them and the
    renewal branch upserts them (#293); a renewal that carried a different
    column set would publish an observation shaped unlike the one it renews,
    and the drain builds the wire payload from exactly these columns.
    """
    return {
        "command_id": blob.command_id,
        "blob_uri": blob.blob_uri,
        "blob_expires_at": blob.blob_expires_at,
        "blob_fingerprint": blob.blob_fingerprint,
        "source_media_type": blob.source_media_type,
        "content_media_type": outcome.content_media_type,
        "spec_fingerprint": outcome.spec_fingerprint,
    }


# ---------------------------------------------------------------------------
# Per-WatchedItem history.
# ---------------------------------------------------------------------------


@dataclass
class WatchedItemResult:
    """Outcome of one check cycle for a WatchedItem."""

    baseline_established: bool = False
    cache_hit: bool = False
    changed: bool = False
    notifications_dispatched: int = 0
    errors: list[str] = field(default_factory=list)
    # `changed` implies an outbox row since #251 (a detected change always
    # enqueues one). This is the other writer: a cache hit that re-announced
    # the latest revision because this cycle's full fetch renewed the blob
    # reference behind it (#293). Never set beside `changed`.
    renewal_enqueued: bool = False
    # Option A (D6, #326): the fingerprint moved because the extractor did, so
    # a revision was written and announced but nobody was notified. Never set
    # beside `changed`.
    rebaselined: bool = False


class ExtractionChange(enum.StrEnum):
    """Why a fingerprint may have moved other than the page changing (D6)."""

    SPEC = "spec"
    PROCESSOR = "processor"


def extraction_change(
    last_rev: ChangeRevision,
    outcome: ExtractionOutcome,
    *,
    known_processor_version: str | None,
) -> ExtractionChange | None:
    """Option A's classifier for a fingerprint change (D6, #326).

    * The spec that bound moved → ``SPEC``: notify, labelled. Checked first,
      because re-baselining a spec edit would mask a coincident page change.
    * The extractor moved, the spec did not → ``PROCESSOR``: re-baseline
      silently.
    * Otherwise ``None``: a content change, notified as ever.

    Spec identity is compared against the previous **revision**. Processor
    identity is compared against the **item's** (``known_processor_version``,
    read before this outcome moves it): an equal digest under a new version
    refreshes the item and never rewrites a revision, so the revision's own
    version may be older than the extractor the item has already met. ``None``
    on either side is *unknown* and triggers neither.
    """
    if (
        last_rev.spec_fingerprint is not None
        and outcome.spec_fingerprint is not None
        and last_rev.spec_fingerprint != outcome.spec_fingerprint
    ):
        return ExtractionChange.SPEC
    if (
        known_processor_version is not None
        and outcome.processor_version is not None
        and known_processor_version != outcome.processor_version
    ):
        return ExtractionChange.PROCESSOR
    return None


async def _renew_blob_reference(
    session: AsyncSession,
    watched_item: WatchedItem,
    rev: ChangeRevision,
    *,
    blob: BlobProvenance,
    outcome: ExtractionOutcome,
    now: datetime,
) -> bool:
    """Re-announce ``rev`` under this cycle's blob reference (#293).

    Replicator re-references the blob on every full re-fetch of unchanged bytes
    and publishes a fresh fact with a later ``blob_expires_at``; Archiver's row
    for the pair took only the first observation's horizon, so a stable item
    became unreplicable once it passed — while the bytes sat alive. The renewal
    is the same row shape for the same revision: the drain publishes it under
    the same envelope key, and Archiver dedupes on the pair and moves the
    horizon forward-only (archiver#201).

    An upsert on ``change_revision_id``, which is unique, rather than an ORM
    add: a row the drain has not published yet takes the newer provenance in
    place and is pulled to ``now``, and a dead-lettered row is revived — that
    verdict was about values this observation has replaced. ``attempts`` is
    left alone; the history it records is still true. Done as one statement so
    a drain holding the row ``FOR UPDATE`` resolves in Postgres: the insert
    waits, then either updates the row the drain kept or inserts fresh after
    the drain deleted it, instead of a stale ORM UPDATE matching zero rows.

    Returns whether it renewed. **A renewal may only ever improve a queued
    row** (CR 1): this is the one writer that overwrites provenance rather than
    creating it, so a reference missing a field the wire requires would replace
    a publishable row with one the drain dead-letters — a real revision lost to
    a refresh, where the change path's equivalent gap costs only an observation
    that never existed. Declining also keeps a row that could only dead-letter
    out of the outbox when there is nothing there to protect. Unreachable
    today: a command is unsendable without a ``blob_uri``, and the consumer
    writes ``media_type`` in the same upsert — but nothing else enforces it,
    and the asymmetry is what makes it worth a guard.
    """
    provenance = _provenance_columns(blob, outcome)
    missing = [name for name in WIRE_REQUIRED_PROVENANCE_FIELDS if provenance[name] is None]
    if missing:
        logger.warning(
            "blob reference is unpublishable — declining to renew",
            extra={
                "watched_item_id": str(watched_item.id),
                "change_revision_id": str(rev.id),
                "command_id": blob.command_id,
                "missing": missing,
            },
        )
        return False

    await session.execute(
        pg_insert(PendingArchiverSync)
        .values(
            change_revision_id=rev.id,
            watched_item_id=watched_item.id,
            next_attempt_at=now,
            **provenance,
        )
        .on_conflict_do_update(
            index_elements=["change_revision_id"],
            set_={
                **provenance,
                "next_attempt_at": now,
                "dead_lettered_at": None,
                "last_error": None,
            },
        )
    )
    logger.info(
        "blob reference renewed — re-announcing the latest revision",
        extra={
            "watched_item_id": str(watched_item.id),
            "change_revision_id": str(rev.id),
            "command_id": blob.command_id,
            "blob_expires_at": (
                format_utc_iso(blob.blob_expires_at) if blob.blob_expires_at else None
            ),
        },
    )
    return True


async def apply_extraction_outcome(
    session: AsyncSession,
    watched_item: WatchedItem,
    outcome: ExtractionOutcome,
    *,
    blob: BlobProvenance,
) -> WatchedItemResult:
    """Compare one outcome against the item's history and act on it.

    Called with what a ``content.derived`` fact reported (#326):

    a. First run: insert a baseline ChangeRevision, no notification.
    b. Same fingerprint: cache hit — no revision, no notification. If the
       latest revision has already been announced (it has an older sibling,
       so the change path enqueued it), upsert a PendingArchiverSync for it
       carrying this cycle's blob reference (#293). The baseline is never
       announced here.
    c. Changed: insert a new ChangeRevision, enqueue PendingArchiverSync, then
       Option A (``extraction_change``): an extractor-only move writes
       ``CHECK_REBASELINED`` and notifies nobody; anything else dispatches
       CHANGE_DETECTED once for the WatchedItem, labelled when the spec moved.

    ``last_changed_at`` is updated on a notified change; ``last_checked_at``
    and ``health_status`` are the caller's (``close_succeeded``). ``blob``
    carries the correlated ``content.blobs`` fact onto the outbox row (#253).

    ``watched_item.processor_version`` is read as the comparison base and then
    set to the outcome's, on every branch: an unchanged outcome under a new
    extractor teaches the item that version without a revision.
    """
    now = datetime.now(UTC)
    known_processor_version = watched_item.processor_version
    watched_item.processor_version = outcome.processor_version

    # Two rows, not one: the second answers whether the latest revision is the
    # baseline. The baseline is the one revision the change branch never
    # enqueued, and the renewal below must not be Archiver's *first*
    # observation of a pair — that is a registry insert plus an `info.changes`
    # event, not a horizon refresh. "Has an older sibling" is the same test the
    # dashboard uses to keep baselines out of `changes_today`.
    latest_revs = (
        (
            await session.execute(
                select(ChangeRevision)
                .where(ChangeRevision.watched_item_id == watched_item.id)
                .order_by(ChangeRevision.captured_at.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    last_rev = latest_revs[0] if latest_revs else None
    latest_is_announced = len(latest_revs) > 1

    if last_rev is None:
        # First run: establish baseline — no notification.
        session.add(
            ChangeRevision(
                watched_item_id=watched_item.id,
                content_fingerprint=outcome.content_fingerprint,
                captured_at=now,
                content_size_bytes=outcome.content_size_bytes,
                schema_version=outcome.schema_version,
                spec_fingerprint=outcome.spec_fingerprint,
                processor_version=outcome.processor_version,
            )
        )
        return WatchedItemResult(baseline_established=True)

    if last_rev.content_fingerprint == outcome.content_fingerprint:
        # Cache hit. Bytes arrived, so Replicator renewed the blob reference
        # behind this revision; tell Archiver, or its stored horizon freezes at
        # the first observation (#293). Bounded by full fetches — a 304 never
        # reaches this function.
        if not latest_is_announced:
            return WatchedItemResult(cache_hit=True)
        renewed = await _renew_blob_reference(
            session, watched_item, last_rev, blob=blob, outcome=outcome, now=now
        )
        return WatchedItemResult(cache_hit=True, renewal_enqueued=renewed)

    # Fingerprint changed: insert new ChangeRevision.
    rev = ChangeRevision(
        watched_item_id=watched_item.id,
        content_fingerprint=outcome.content_fingerprint,
        captured_at=now,
        content_size_bytes=outcome.content_size_bytes,
        schema_version=outcome.schema_version,
        spec_fingerprint=outcome.spec_fingerprint,
        processor_version=outcome.processor_version,
    )
    session.add(rev)
    await session.flush()  # populate rev.id before the outbox row references it

    # The outbox row is the observation, so it carries where the bytes came from
    # (#253): the blob facts as Replicator stated them, plus the identity of the
    # spec they were extracted under. Snapshotted here rather than joined at
    # drain time — the FetchCommand's lifecycle is not the outbox row's, and the
    # values are free at this point because the apply path already holds them.
    # No scratch copy: the durable-ish blob is Replicator's, at blob_uri, and
    # writing our own copy of bytes it already stored only to report *that* path
    # was three moving parts doing nothing the blob URI does.
    session.add(
        PendingArchiverSync(
            change_revision_id=rev.id,
            watched_item_id=watched_item.id,
            next_attempt_at=now,
            **_provenance_columns(blob, outcome),
        )
    )

    change = extraction_change(last_rev, outcome, known_processor_version=known_processor_version)
    if change is ExtractionChange.PROCESSOR:
        # Option A (D6): the extractor moved and the spec did not. The revision
        # stands — it is the baseline the next outcome compares against, and
        # Archiver records it like any other — but nothing is sent, and the
        # change clock stays where the last *content* change left it. Accepted
        # residual: a real change coincident with the upgrade is absorbed.
        audit(
            session,
            EventType.CHECK_REBASELINED,
            watched_item_id=str(watched_item.id),
            change_revision_id=str(rev.id),
            previous_fingerprint=last_rev.content_fingerprint,
            content_fingerprint=outcome.content_fingerprint,
            previous_processor_version=known_processor_version,
            processor_version=outcome.processor_version,
        )
        logger.info(
            "extractor change moved the fingerprint — re-baselined without notifying",
            extra={
                "watched_item_id": str(watched_item.id),
                "change_revision_id": str(rev.id),
                "previous_processor_version": known_processor_version,
                "processor_version": outcome.processor_version,
            },
        )
        return WatchedItemResult(rebaselined=True)

    # #349: the change before this one, for the email's PREVIOUS CHANGE —
    # read before it is overwritten, since `last_changed_at` is about to be
    # this change. None on an item's first change: a baseline never sets it.
    previous_changed_at = watched_item.last_changed_at
    watched_item.last_changed_at = now

    # #191: dispatch CHANGE_DETECTED once for the WatchedItem (the monitored entity).
    # No registry id in the metadata: Archiver allocates it on its side of
    # content.revisions and never tells us, so the key was permanently null
    # (#253; the column it mirrored was dropped in #261). `extraction_changed`
    # is Option A's label: "spec" when the bound spec moved, else None — a
    # "processor" change never reaches a notification. The two fingerprints
    # are the two texts' storage addresses (#222): the diff reads them back.
    change_meta: dict = {
        "change_revision_id": str(rev.id),
        "previous_fingerprint": last_rev.content_fingerprint,
        "current_fingerprint": outcome.content_fingerprint,
        "extraction_changed": change.value if change is not None else None,
        **watched_item_event_base_metadata(watched_item),
    }
    if previous_changed_at is not None:
        change_meta["previous_changed_at"] = format_utc_iso(previous_changed_at)

    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watched_item_id=str(watched_item.id),
        item_name=watched_item.name,
        item_url=watched_item.effective_url,
        occurred_at=now,
        metadata=change_meta,
    )
    await dispatch_event_notifications(session=session, event=event)

    return WatchedItemResult(changed=True, notifications_dispatched=1)
