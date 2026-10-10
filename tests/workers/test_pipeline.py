"""Tests for the history half of a check: ``apply_extraction_outcome``.

The processor extracts (#326, #350); these tests hand the comparison an
outcome as a ``content.derived`` fact reports it — baseline, cache hit (#293
renewal), change, and Option A.
"""

import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from notifier_client.types import DispatchOutStatus
from sqlalchemy import select
from ulid import ULID

from src.core.fetch_commands import create_fetch_command
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.change_revision import ChangeRevision
from src.core.models.notification_template import VISIBILITY_GLOBAL, NotificationTemplate
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.process_command import ProcessCommandStatus
from src.core.process_commands import create_process_command
from src.workers.pipeline import (
    BlobProvenance,
    ExtractionOutcome,
    WatchedItemResult,
    apply_extraction_outcome,
)
from tests.conftest import make_watched_item

_SPEC_FULL_PAGE = {"schema_version": 1, "extraction": {"algorithm": "full_page"}}
SPEC_FP = "spec1:sha256:" + "11" * 32
VERSION = "0.19.7+1"
BASE_FP = "sha256:" + "aa" * 32
NEXT_FP = "sha256:" + "bb" * 32


def _outcome(fingerprint=BASE_FP, *, spec=SPEC_FP, version=VERSION) -> ExtractionOutcome:
    return ExtractionOutcome(
        content_fingerprint=fingerprint,
        content_size_bytes=10,
        schema_version=1,
        spec_fingerprint=spec,
        processor_version=version,
    )


# Provenance is required since the cutover — an observation Watcher cannot say
# where it came from has nothing to publish. Most tests do not care about the
# values, only that the comparison has some.
_BLOB = BlobProvenance(
    command_id="01KZMNQR9B5CQZ1CRGR1E393R6",
    blob_uri="gs://co-gcs-blobs/blobs/test.bin",
    source_media_type="text/html",
)


@pytest.mark.integration
class TestApplyExtractionOutcome:
    async def test_first_run_establishes_baseline_no_notification(self, db_session):
        """First run: ChangeRevision inserted, no CHANGE_DETECTED notification."""
        wi = await make_watched_item(db_session, name="Baseline")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)

        assert isinstance(result, WatchedItemResult)
        assert result.baseline_established is True
        assert result.cache_hit is False
        assert result.changed is False
        assert result.notifications_dispatched == 0
        mock_dispatch.assert_not_awaited()

        revs = (
            (
                await db_session.execute(
                    select(ChangeRevision).where(ChangeRevision.watched_item_id == wi.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(revs) == 1
        assert revs[0].content_fingerprint == BASE_FP

    async def test_same_fingerprint_is_cache_hit_no_new_revision(self, db_session):
        """Second run with same content: cache hit, no new ChangeRevision."""
        wi = await make_watched_item(db_session, name="CacheHit")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        # Establish baseline
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        await db_session.flush()

        # Same content
        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)

        assert result.cache_hit is True
        assert result.changed is False
        assert result.notifications_dispatched == 0
        mock_dispatch.assert_not_awaited()

        revs = (
            (
                await db_session.execute(
                    select(ChangeRevision).where(ChangeRevision.watched_item_id == wi.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(revs) == 1  # only the baseline

    async def test_changed_fingerprint_inserts_revision_and_notifies(self, db_session):
        """Content change: new ChangeRevision + CHANGE_DETECTED for the WatchedItem."""
        wi = await make_watched_item(db_session, name="Changed")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_BLOB)

        assert result.changed is True
        assert result.notifications_dispatched == 1
        mock_dispatch.assert_awaited_once()

        event = mock_dispatch.call_args.kwargs["event"]
        assert event.event_type.value == "change_detected"
        assert event.watched_item_id == str(wi.id)
        assert event.item_url == "https://example.com"
        assert "change_revision_id" in event.metadata

        revs = (
            (
                await db_session.execute(
                    select(ChangeRevision).where(ChangeRevision.watched_item_id == wi.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(revs) == 2

    async def test_change_updates_last_changed_at(self, db_session):
        """last_changed_at is set on WatchedItem when fingerprint changes."""
        wi = await make_watched_item(db_session, name="Timestamps")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        assert wi.last_changed_at is None
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        assert wi.last_changed_at is None  # baseline: no change event

        before = datetime.now(UTC)
        await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_BLOB)
        assert wi.last_changed_at is not None
        assert wi.last_changed_at >= before

    async def test_archiver_sync_enqueued_on_every_change(self, db_session):
        """#251: every detected change enqueues a PendingArchiverSync — no guard.

        archiver_info_source_id is NOT NULL, so the old conditional could only
        ever drop a captured revision on the floor.
        """
        wi = await make_watched_item(db_session, name="WithSync")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        await db_session.flush()
        result = await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_BLOB)
        await db_session.flush()

        assert result.changed is True
        syncs = (
            (
                await db_session.execute(
                    select(PendingArchiverSync).where(PendingArchiverSync.watched_item_id == wi.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(syncs) == 1
        # No scratch copy since the cutover: the row points at Replicator's blob.
        assert syncs[0].blob_uri == _BLOB.blob_uri

    async def test_dispatches_once_per_watched_item(self, db_session):
        """#191: CHANGE_DETECTED fires exactly once for the WatchedItem (the entity)."""
        wi = await make_watched_item(db_session, name="Item1")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        await db_session.flush()

        dispatched_events = []

        async def capture(*, session, event):
            dispatched_events.append(event)

        with patch("src.workers.pipeline.dispatch_event_notifications", side_effect=capture):
            result = await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_BLOB)

        assert result.notifications_dispatched == 1
        assert len(dispatched_events) == 1
        assert dispatched_events[0].watched_item_id == str(wi.id)


@pytest.mark.integration
class TestRevisionExtractionIdentity:
    """#324: a revision records which spec and which processor produced it.

    Both were computed and discarded with the outbox row. The design's Option A
    reads them off the previous and current revisions to tell a spec-induced
    or processor-induced fingerprint move from a content change; NULL on a row
    written before this landed means *unknown* and triggers neither.
    """

    async def _revisions(self, db_session, wi):
        return (
            (
                await db_session.execute(
                    select(ChangeRevision)
                    .where(ChangeRevision.watched_item_id == wi.id)
                    .order_by(ChangeRevision.captured_at)
                )
            )
            .scalars()
            .all()
        )

    async def test_baseline_carries_spec_and_processor_identity(self, db_session):
        wi = await make_watched_item(
            db_session, name="Identity baseline", source_specs=[_SPEC_FULL_PAGE]
        )
        await db_session.flush()

        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        await db_session.flush()

        (baseline,) = await self._revisions(db_session, wi)
        assert baseline.spec_fingerprint == SPEC_FP
        assert baseline.processor_version == VERSION

    async def test_unknown_identity_stores_null_not_a_lost_revision(self, db_session):
        # The processor reports no spec identity when it cannot derive one; the
        # identity is a diagnostic, so the revision is still written — with
        # NULL, which Option A reads as unknown.
        wi = await make_watched_item(
            db_session, name="Identity unknown", source_specs=[_SPEC_FULL_PAGE]
        )
        await db_session.flush()

        await apply_extraction_outcome(
            db_session, wi, _outcome(BASE_FP, spec=None, version=None), blob=_BLOB
        )
        await db_session.flush()

        (baseline,) = await self._revisions(db_session, wi)
        assert baseline.spec_fingerprint is None
        assert baseline.processor_version is None


@pytest.mark.integration
class TestOutboxProvenance:
    """#253: the outbox row is the observation, so it carries where it came from."""

    async def test_change_records_blob_provenance_and_spec_identity(self, db_session):
        wi = await make_watched_item(db_session, name="Provenance")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_FULL_PAGE]
        await db_session.flush()

        blob = BlobProvenance(
            command_id="01J9ZZZZZZZZZZZZZZZZZZZZZZ",
            blob_uri="file:///var/lib/replicator/blobs/abc.bin",
            source_media_type="text/html",
            blob_expires_at=datetime(2026, 8, 16, tzinfo=UTC),
            blob_fingerprint="c" * 64,
        )

        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=blob)
        await db_session.flush()
        await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=blob)
        await db_session.flush()

        row = (
            (
                await db_session.execute(
                    select(PendingArchiverSync).where(PendingArchiverSync.watched_item_id == wi.id)
                )
            )
            .scalars()
            .one()
        )
        assert row.command_id == blob.command_id
        assert row.blob_uri == blob.blob_uri
        assert row.source_media_type == "text/html"
        assert row.blob_expires_at == blob.blob_expires_at
        assert row.blob_fingerprint == blob.blob_fingerprint
        assert row.spec_fingerprint == SPEC_FP
        assert row.content_media_type == "text/plain; charset=utf-8"


# Two full fetches of the same bytes: Replicator re-references the blob on the
# second and publishes a fresh fact with a later horizon (replicator
# docs/STORAGE.md). Same URI, later expiry, new command. The digests differ on
# purpose: unchanged *extracted* text does not mean unchanged raw bytes, and
# Archiver pairs the digest with the URI it arrives beside (archiver#280), so a
# renewal that kept the old one would fail every persist (#329).
_FIRST_BLOB = BlobProvenance(
    command_id="01J9AAAAAAAAAAAAAAAAAAAAAA",
    blob_uri="gs://co-gcs-blobs/abc",
    source_media_type="text/html",
    blob_expires_at=datetime(2026, 9, 1, tzinfo=UTC),
    blob_fingerprint="f" * 64,
)
_RENEWED_BLOB = BlobProvenance(
    command_id="01J9BBBBBBBBBBBBBBBBBBBBBB",
    blob_uri="gs://co-gcs-blobs/abc",
    source_media_type="text/html",
    blob_expires_at=datetime(2026, 9, 8, tzinfo=UTC),
    blob_fingerprint="d" * 64,
)


@pytest.mark.integration
class TestBlobReferenceRenewal:
    """#293: an unchanged fingerprint re-announces the latest revision when a
    full fetch renews the blob reference behind it.

    Archiver's ``content_cache_expires_at`` for a pair froze at the first
    observation because this branch announced nothing, so a stable item became
    unreplicable once that horizon passed — while Replicator kept renewing the
    blob on every full re-fetch. The renewal is the same outbox row shape for
    the same ``ChangeRevision``; Archiver dedupes on the pair and moves the
    horizon forward-only (archiver#201), so the wire is unchanged.
    """

    async def _item(self, db_session):
        wi = await make_watched_item(db_session, name="Renewal")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_FULL_PAGE]
        await db_session.flush()
        return wi

    async def _outbox_rows(self, db_session, wi) -> list[PendingArchiverSync]:
        # populate_existing: the renewal is a Core upsert, so an identity the
        # test loaded earlier must be re-read from the row, not the map.
        result = await db_session.execute(
            select(PendingArchiverSync)
            .where(PendingArchiverSync.watched_item_id == wi.id)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def _revisions(self, db_session, wi) -> list[ChangeRevision]:
        result = await db_session.execute(
            select(ChangeRevision)
            .where(ChangeRevision.watched_item_id == wi.id)
            .order_by(ChangeRevision.captured_at.desc())
        )
        return list(result.scalars().all())

    async def test_cache_hit_after_a_change_re_announces_the_latest_revision(self, db_session):
        """The drained case: the change's row is gone, the renewal enqueues a new
        one for the same revision, carrying the fresh fact's provenance."""
        wi = await self._item(db_session)
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_FIRST_BLOB)
        await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        await db_session.delete(queued)  # the drain published it
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await apply_extraction_outcome(
                db_session, wi, _outcome(NEXT_FP), blob=_RENEWED_BLOB
            )
        await db_session.flush()

        assert result.cache_hit is True
        assert result.changed is False
        assert result.renewal_enqueued is True
        mock_dispatch.assert_not_awaited()

        revs = await self._revisions(db_session, wi)
        assert len(revs) == 2  # a renewal is not a revision
        (row,) = await self._outbox_rows(db_session, wi)
        assert row.change_revision_id == revs[0].id
        assert row.command_id == _RENEWED_BLOB.command_id
        assert row.blob_uri == _RENEWED_BLOB.blob_uri
        assert row.blob_expires_at == _RENEWED_BLOB.blob_expires_at
        assert row.blob_fingerprint == _RENEWED_BLOB.blob_fingerprint
        assert row.source_media_type == "text/html"
        assert row.content_media_type == "text/plain; charset=utf-8"
        assert row.spec_fingerprint == SPEC_FP
        assert row.dead_lettered_at is None

    async def test_a_baseline_is_never_announced_by_a_cache_hit(self, db_session):
        """The baseline is the one revision the change path never enqueued.
        Announcing it here would be a *first* observation of the pair — a
        registry insert and an ``info.changes`` event — not a renewal."""
        wi = await self._item(db_session)
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_FIRST_BLOB)
        await db_session.flush()

        result = await apply_extraction_outcome(
            db_session, wi, _outcome(BASE_FP), blob=_RENEWED_BLOB
        )
        await db_session.flush()

        assert result.cache_hit is True
        assert result.renewal_enqueued is False
        assert await self._outbox_rows(db_session, wi) == []

    async def test_renewal_upserts_a_still_queued_row(self, db_session):
        """``change_revision_id`` is unique: a renewal of a row the drain has not
        published yet updates its provenance in place, never adds a second."""
        wi = await self._item(db_session)
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_FIRST_BLOB)
        await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        queued_id = queued.id

        result = await apply_extraction_outcome(
            db_session, wi, _outcome(NEXT_FP), blob=_RENEWED_BLOB
        )
        await db_session.flush()

        assert result.renewal_enqueued is True
        (row,) = await self._outbox_rows(db_session, wi)
        assert row.id == queued_id
        assert row.command_id == _RENEWED_BLOB.command_id
        assert row.blob_expires_at == _RENEWED_BLOB.blob_expires_at
        assert row.blob_fingerprint == _RENEWED_BLOB.blob_fingerprint
        assert row.next_attempt_at <= datetime.now(UTC)

    async def test_renewal_revives_a_dead_lettered_row(self, db_session):
        """Dead-lettering is for a payload that is unbuildable *from the row's
        values*; a renewal replaces those values, so the verdict no longer
        applies. The attempt history stays — it is still true."""
        wi = await self._item(db_session)
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_FIRST_BLOB)
        await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        queued.attempts = 1
        queued.last_error = "unbuildable_payload: ValidationError(...)"
        queued.dead_lettered_at = datetime.now(UTC)
        queued.next_attempt_at = datetime(2099, 1, 1, tzinfo=UTC)
        await db_session.flush()

        result = await apply_extraction_outcome(
            db_session, wi, _outcome(NEXT_FP), blob=_RENEWED_BLOB
        )
        await db_session.flush()

        assert result.renewal_enqueued is True
        (row,) = await self._outbox_rows(db_session, wi)
        assert row.dead_lettered_at is None
        assert row.last_error is None
        assert row.next_attempt_at <= datetime.now(UTC)
        assert row.attempts == 1
        assert row.blob_expires_at == _RENEWED_BLOB.blob_expires_at
        assert row.blob_fingerprint == _RENEWED_BLOB.blob_fingerprint

    async def test_an_unpublishable_reference_never_degrades_a_queued_row(self, db_session):
        """CR 1: the renewal is the only writer that can *overwrite* provenance.

        The change path can only ever create a row, so a wire-required field it
        lacks costs one observation that never existed. Here the same gap would
        replace a publishable row with one the drain dead-letters — a real
        revision lost to a refresh. Unreachable today (a command is unsendable
        without a URI, and the consumer writes ``media_type`` alongside it), so
        this pins the invariant rather than a live path.
        """
        wi = await self._item(db_session)
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_FIRST_BLOB)
        await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        queued_id = queued.id

        typeless = BlobProvenance(
            command_id=_RENEWED_BLOB.command_id,
            blob_uri=_RENEWED_BLOB.blob_uri,
            source_media_type=None,
            blob_expires_at=_RENEWED_BLOB.blob_expires_at,
        )
        result = await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=typeless)
        await db_session.flush()

        assert result.cache_hit is True
        assert result.renewal_enqueued is False
        (row,) = await self._outbox_rows(db_session, wi)
        assert row.id == queued_id
        assert row.source_media_type == "text/html"
        assert row.blob_expires_at == _FIRST_BLOB.blob_expires_at
        assert row.blob_fingerprint == _FIRST_BLOB.blob_fingerprint  # the pair stays whole

    async def test_an_unpublishable_reference_enqueues_nothing_when_drained(self, db_session):
        """The same guard with no row to protect: a renewal that could only
        dead-letter is not worth queueing."""
        wi = await self._item(db_session)
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_FIRST_BLOB)
        await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        await db_session.delete(queued)
        await db_session.flush()

        uriless = BlobProvenance(
            command_id=_RENEWED_BLOB.command_id,
            blob_uri=None,
            source_media_type="text/html",
            blob_expires_at=_RENEWED_BLOB.blob_expires_at,
        )
        result = await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=uriless)
        await db_session.flush()

        assert result.renewal_enqueued is False
        assert await self._outbox_rows(db_session, wi) == []


@pytest.mark.integration
class TestChangeEventCarriesTheDiffAddresses:
    """#222: a change names both texts by their storage address — the
    fingerprint *is* where the processor keeps the canonical text."""

    async def _changed(self, db_session, name):
        wi = await make_watched_item(db_session, name=name, source_specs=[_SPEC_FULL_PAGE])
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        await db_session.flush()
        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as dispatch:
            await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_BLOB)
        baseline, change = (
            (
                await db_session.execute(
                    select(ChangeRevision)
                    .where(ChangeRevision.watched_item_id == wi.id)
                    .order_by(ChangeRevision.captured_at)
                )
            )
            .scalars()
            .all()
        )
        return dispatch, baseline, change

    async def test_previous_and_current_fingerprints(self, db_session):
        dispatch, baseline, change = await self._changed(db_session, "Diff addresses")
        meta = dispatch.call_args.kwargs["event"].metadata
        assert meta["previous_fingerprint"] == baseline.content_fingerprint
        assert meta["current_fingerprint"] == change.content_fingerprint
        assert "content_fingerprint" not in meta

    async def test_a_first_change_has_no_previous_change(self, db_session):
        """#349: a baseline never sets ``last_changed_at``, so there is none."""
        dispatch, _baseline, _change = await self._changed(db_session, "First change")
        meta = dispatch.call_args.kwargs["event"].metadata
        assert "previous_changed_at" not in meta

    async def test_a_later_change_names_the_one_before_it(self, db_session):
        """#349: ``last_changed_at`` is this change by the time the event is
        built; the email's PREVIOUS CHANGE needs the value it replaced."""
        wi = await make_watched_item(
            db_session, name="Second change", source_specs=[_SPEC_FULL_PAGE]
        )
        await apply_extraction_outcome(db_session, wi, _outcome(BASE_FP), blob=_BLOB)
        first = datetime(2026, 10, 6, 21, 7, tzinfo=UTC)
        wi.last_changed_at = first
        await db_session.flush()
        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as dispatch:
            await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_BLOB)
        meta = dispatch.call_args.kwargs["event"].metadata
        assert meta["previous_changed_at"] == "2026-10-06T21:07:00Z"
        assert wi.last_changed_at > first


async def _audits_of(db_session, event_type, wi) -> list[AuditLog]:
    stmt = select(AuditLog).where(
        AuditLog.event_type == event_type,
        AuditLog.payload["watched_item_id"].astext == str(wi.id),
    )
    return list((await db_session.execute(stmt)).scalars())


def _address(text: bytes) -> str:
    return f"sha256:{hashlib.sha256(text).hexdigest()}"


@pytest.mark.integration
class TestChangeDiffEndToEnd:
    """#222 acceptance: a change with Full diff enabled delivers the diff.

    Nothing between the comparison and the notifier is mocked: the real
    dispatcher selects the template, the real loader finds both texts through
    the processor's ``process_commands`` rows (under its savepoint), and the
    real renderer fences the diff. Only the two edges are stubbed — the
    notifier client and the GCS read.
    """

    async def _stored(self, db_session, wi, text: bytes) -> str:
        """A completed answer naming where the processor stored ``text``."""
        fetch = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        fetch.blob_uri, fetch.content_fingerprint = _BLOB.blob_uri, "61" * 32
        await db_session.flush()
        answered = await create_process_command(db_session, fetch, wi, now=datetime.now(UTC))
        answered.status = ProcessCommandStatus.COMPLETED
        answered.output_digest = _address(text)
        answered.output_uri = f"gs://co-gcs-processor/blobs/{hashlib.sha256(text).hexdigest()}.bin"
        answered.output_size_bytes = len(text)
        await db_session.flush()
        return answered.output_uri

    async def test_full_diff_reaches_the_notifier(self, db_session):
        previous, current = b"Hello world", b"Content changed"
        wi = await make_watched_item(db_session, name="Diff e2e", source_specs=[_SPEC_FULL_PAGE])
        await apply_extraction_outcome(db_session, wi, _outcome(_address(previous)), blob=_BLOB)
        stored = {
            await self._stored(db_session, wi, previous): previous,
            await self._stored(db_session, wi, current): current,
        }
        db_session.add(
            NotificationTemplate(
                title="Diff e2e",
                visibility=VISIBILITY_GLOBAL,
                remote_channel_id=str(ULID()),
                channel_hint="json",
                events=["change_detected"],
                content_config={"default": {"include_diff_full": True}},
            )
        )
        await db_session.flush()

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.dispatch.return_value = MagicMock(
            id=str(ULID()), status=DispatchOutStatus.SUCCEEDED, attempts=[]
        )
        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch(
                "src.core.notifications.diff_loader.aread_blob",
                new=AsyncMock(side_effect=lambda u: stored[u]),
            ),
        ):
            result = await apply_extraction_outcome(
                db_session, wi, _outcome(_address(current)), blob=_BLOB
            )
        await db_session.flush()

        assert result.changed is True
        body = client.dispatch.call_args.kwargs["body_template"]
        fenced = body.split("\n\n", 1)[1]
        assert fenced == "```diff\n- Hello world\n+ Content changed\n```"
        assert "DIFF: unavailable" not in body
        (dispatched,) = await _audits_of(db_session, EventType.NOTIFICATION_DISPATCHED, wi)
        assert dispatched.payload["results"][0]["success"] is True


@pytest.mark.integration
class TestOptionA:
    """D6: a fingerprint move the extractor caused is not a content change (#326).

    The outcome is applied as the processor reports it (``apply_extraction_
    outcome``), so these tests hand it the identity fields directly. Spec
    identity is compared against the previous *revision*; processor identity
    against ``WatchedItem.processor_version`` read before the outcome moves it
    — an equal digest under a new version refreshes the item, never the
    revision, so the revision's own version can be stale.
    """

    OTHER_SPEC_FP = "spec1:sha256:" + "22" * 32

    async def _baselined(self, db_session, name, **outcome_kwargs):
        wi = await make_watched_item(db_session, name=name, source_specs=[_SPEC_FULL_PAGE])
        await db_session.flush()
        await apply_extraction_outcome(
            db_session, wi, _outcome(BASE_FP, **outcome_kwargs), blob=_BLOB
        )
        await db_session.flush()
        return wi

    async def _revisions(self, db_session, wi):
        stmt = (
            select(ChangeRevision)
            .where(ChangeRevision.watched_item_id == wi.id)
            .order_by(ChangeRevision.captured_at)
        )
        return list((await db_session.execute(stmt)).scalars().all())

    async def _rebaselined_audits(self, db_session, wi):
        stmt = select(AuditLog).where(
            AuditLog.event_type == EventType.CHECK_REBASELINED,
            AuditLog.payload["watched_item_id"].astext == str(wi.id),
        )
        return list((await db_session.execute(stmt)).scalars().all())

    async def test_baseline_records_the_items_processor_version(self, db_session):
        wi = await self._baselined(db_session, "OptionA baseline")
        assert wi.processor_version == "0.19.7+1"

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_processor_change_alone_re_baselines_silently(self, dispatch, db_session):
        wi = await self._baselined(db_session, "OptionA processor")
        before = wi.last_changed_at

        result = await apply_extraction_outcome(
            db_session, wi, _outcome(NEXT_FP, version="0.20.0+1"), blob=_BLOB
        )
        await db_session.flush()

        assert result.rebaselined is True
        assert result.changed is False
        assert result.notifications_dispatched == 0
        dispatch.assert_not_awaited()
        _baseline, rebaseline = await self._revisions(db_session, wi)
        assert rebaseline.content_fingerprint == NEXT_FP
        assert rebaseline.processor_version == "0.20.0+1"
        assert wi.processor_version == "0.20.0+1"
        # Not a content change: the item's change clock does not move.
        assert wi.last_changed_at == before
        (event,) = await self._rebaselined_audits(db_session, wi)
        assert event.payload["previous_processor_version"] == "0.19.7+1"
        assert event.payload["processor_version"] == "0.20.0+1"
        assert event.payload["change_revision_id"] == str(rebaseline.id)

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_re_baseline_is_still_announced_to_archiver(self, _dispatch, db_session):
        # The derived text moved, so the registry's record of it must move too;
        # only the notification is withheld.
        wi = await self._baselined(db_session, "OptionA outbox")
        await apply_extraction_outcome(
            db_session, wi, _outcome(NEXT_FP, version="0.20.0+1"), blob=_BLOB
        )
        await db_session.flush()
        _baseline, rebaseline = await self._revisions(db_session, wi)
        stmt = select(PendingArchiverSync).where(
            PendingArchiverSync.change_revision_id == rebaseline.id
        )
        assert (await db_session.execute(stmt)).scalar_one_or_none() is not None

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_spec_change_notifies_with_a_label(self, dispatch, db_session):
        wi = await self._baselined(db_session, "OptionA spec")

        result = await apply_extraction_outcome(
            db_session, wi, _outcome(NEXT_FP, spec=self.OTHER_SPEC_FP), blob=_BLOB
        )

        assert result.changed is True
        event = dispatch.call_args.kwargs["event"]
        assert event.metadata["extraction_changed"] == "spec"

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_spec_change_beside_a_processor_change_still_notifies(
        self, dispatch, db_session
    ):
        # Re-baselining here would let a spec edit mask a coincident page change.
        wi = await self._baselined(db_session, "OptionA both")

        result = await apply_extraction_outcome(
            db_session,
            wi,
            _outcome(NEXT_FP, spec=self.OTHER_SPEC_FP, version="0.20.0+1"),
            blob=_BLOB,
        )

        assert result.changed is True
        assert dispatch.call_args.kwargs["event"].metadata["extraction_changed"] == "spec"
        assert await self._rebaselined_audits(db_session, wi) == []

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_content_change_carries_no_label(self, dispatch, db_session):
        wi = await self._baselined(db_session, "OptionA content")

        result = await apply_extraction_outcome(db_session, wi, _outcome(NEXT_FP), blob=_BLOB)

        assert result.changed is True
        assert dispatch.call_args.kwargs["event"].metadata["extraction_changed"] is None

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_unknown_identity_triggers_neither(self, dispatch, db_session):
        # NULL on either side means unknown: notify as before Option A.
        wi = await self._baselined(db_session, "OptionA unknown", spec=None)
        wi.processor_version = None

        result = await apply_extraction_outcome(
            db_session,
            wi,
            _outcome(NEXT_FP, spec=self.OTHER_SPEC_FP, version="0.20.0+1"),
            blob=_BLOB,
        )

        assert result.changed is True
        assert dispatch.call_args.kwargs["event"].metadata["extraction_changed"] is None

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_an_equal_digest_refreshes_the_items_version_only(self, dispatch, db_session):
        wi = await self._baselined(db_session, "OptionA refresh")

        result = await apply_extraction_outcome(
            db_session, wi, _outcome(BASE_FP, version="0.20.0+1"), blob=_BLOB
        )
        await db_session.flush()

        assert result.cache_hit is True
        assert wi.processor_version == "0.20.0+1"
        (baseline,) = await self._revisions(db_session, wi)
        # The revision records what wrote it; it is never rewritten.
        assert baseline.processor_version == "0.19.7+1"

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_the_comparison_base_is_the_item_not_the_revision(self, dispatch, db_session):
        # After a refresh the revision still says 0.19.7+1. A change under the
        # version the item already knows is content, not the upgrade.
        wi = await self._baselined(db_session, "OptionA base")
        await apply_extraction_outcome(
            db_session, wi, _outcome(BASE_FP, version="0.20.0+1"), blob=_BLOB
        )
        await db_session.flush()

        result = await apply_extraction_outcome(
            db_session, wi, _outcome(NEXT_FP, version="0.20.0+1"), blob=_BLOB
        )

        assert result.changed is True
        assert result.rebaselined is False
        dispatch.assert_awaited_once()
