"""Tests for pipeline helpers and process_watched_item.

Unit tests: _extraction_config_from_spec, _extract_with_spec.
Integration tests: process_watched_item baseline + change detection paths.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from co_core.pure.extract import derivation_of, spec_fingerprint
from co_core.pure.extract.csv_excel import CsvExcelExtractor
from co_core.pure.extract.html import HtmlExtractor
from co_core.pure.extract.pdf import PdfExtractor
from sqlalchemy import select

from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.workers.pipeline import (
    BlobProvenance,
    ExtractionError,
    WatchedItemResult,
    _extract_and_fingerprint,
    _extract_with_spec,
    _extraction_config_from_spec,
    process_watched_item,
)
from tests.conftest import make_watched_item


class TestExtractionConfigFromSpec:
    def test_full_page_yields_empty_selectors(self):
        config = _extraction_config_from_spec({"extraction": {"algorithm": "full_page"}})
        assert config == {"selectors": []}

    def test_css_selector_yields_single_selector_list(self):
        config = _extraction_config_from_spec(
            {"extraction": {"algorithm": "css", "selector": ".target"}}
        )
        assert config == {"selectors": [".target"]}

    def test_missing_extraction_block_defaults_to_full_page(self):
        config = _extraction_config_from_spec({})
        assert config == {"selectors": []}


class TestExtractWithSpec:
    def test_extracts_html_with_full_page_algorithm(self):
        document = {"extraction": {"algorithm": "full_page"}}
        result = _extract_with_spec(b"<html><body><p>Hello</p></body></html>", document)
        assert len(result.chunks) >= 1
        assert any("Hello" in c.text for c in result.chunks)

    def test_css_selector_filters_to_matching_section(self):
        document = {"extraction": {"algorithm": "css", "selector": ".target"}}
        result = _extract_with_spec(
            b"<html><body><div class='target'>kept</div><div>dropped</div></body></html>",
            document,
        )
        joined = " ".join(c.text for c in result.chunks)
        assert "kept" in joined
        assert "dropped" not in joined


# ---------------------------------------------------------------------------
# Integration tests for process_watched_item
# ---------------------------------------------------------------------------

_HTML = b"<html><body><p>Hello world</p></body></html>"
_HTML_CHANGED = b"<html><body><p>Content changed</p></body></html>"

# Provenance is required since the cutover — an observation Watcher cannot say
# where it came from has nothing to publish. Most tests do not care about the
# values, only that the pipeline has some.
_BLOB = BlobProvenance(
    command_id="01KZMNQR9B5CQZ1CRGR1E393R6",
    blob_uri="file:///var/lib/replicator/blobs/test.bin",
    source_media_type="text/html",
)


@pytest.mark.integration
class TestProcessWatchedItem:
    async def test_first_run_establishes_baseline_no_notification(self, db_session):
        """First run: ChangeRevision inserted, no CHANGE_DETECTED notification."""
        wi = await make_watched_item(db_session, name="Baseline")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)

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
        assert revs[0].content_fingerprint.startswith("sha256:")

    async def test_same_fingerprint_is_cache_hit_no_new_revision(self, db_session):
        """Second run with same content: cache hit, no new ChangeRevision."""
        wi = await make_watched_item(db_session, name="CacheHit")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        # Establish baseline
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()

        # Same content
        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)

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

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await process_watched_item(
                db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB
            )

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
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        assert wi.last_changed_at is None  # baseline: no change event

        before = datetime.now(UTC)
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB)
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

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()
        result = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB)
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

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()

        dispatched_events = []

        async def capture(*, session, event):
            dispatched_events.append(event)

        with patch("src.workers.pipeline.dispatch_event_notifications", side_effect=capture):
            result = await process_watched_item(
                db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB
            )

        assert result.notifications_dispatched == 1
        assert len(dispatched_events) == 1
        assert dispatched_events[0].watched_item_id == str(wi.id)


# ---------------------------------------------------------------------------
# Empty-extraction guard (#258)
# ---------------------------------------------------------------------------

# A spec whose selector matches nothing in _HTML — what selector rot looks like
# once it has reached every alternative in source_specs.
_SPEC_MISSES = {"schema_version": 1, "extraction": {"algorithm": "css", "selector": ".gone"}}
_SPEC_FULL_PAGE = {"schema_version": 1, "extraction": {"algorithm": "full_page"}}


@pytest.mark.integration
class TestEmptyExtractionGuard:
    """#258: an all-empty extraction is a failure, never a content change.

    Unconditional — the guard does not consult prior revisions. Extracting
    nothing is a broken watch whichever side of a baseline it lands on, and the
    alternative is a silent false ``content changed`` on a rotted selector.
    """

    async def test_all_specs_empty_raises_extraction_error(self, db_session):
        """No baseline yet: the empty digest must not become the baseline."""
        wi = await make_watched_item(db_session, name="EmptyFirstRun")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_MISSES]
        await db_session.flush()

        with pytest.raises(ExtractionError):
            await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)

        revs = (
            (
                await db_session.execute(
                    select(ChangeRevision).where(ChangeRevision.watched_item_id == wi.id)
                )
            )
            .scalars()
            .all()
        )
        assert revs == []

    async def test_rot_after_baseline_writes_nothing_and_does_not_notify(self, db_session):
        """The regression that matters: rot must not present as a content change.

        Before #258 this wrote a zero-byte ChangeRevision, enqueued it to
        Archiver, dispatched CHANGE_DETECTED, and left health OK.
        """
        wi = await make_watched_item(db_session, name="EmptyAfterBaseline")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_FULL_PAGE]
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()

        # Selectors rot: every spec now misses.
        wi.source_specs = [_SPEC_MISSES]
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            with pytest.raises(ExtractionError):
                await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)

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
        assert len(revs) == 1  # the baseline only

        syncs = (
            (
                await db_session.execute(
                    select(PendingArchiverSync).where(PendingArchiverSync.watched_item_id == wi.id)
                )
            )
            .scalars()
            .all()
        )
        assert syncs == []
        assert wi.last_changed_at is None

    async def test_fallback_to_a_later_non_empty_spec_still_succeeds(self, db_session):
        """The guard fires on exhaustion, not on any single spec missing."""
        wi = await make_watched_item(db_session, name="FallbackStillWorks")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_MISSES, _SPEC_FULL_PAGE]
        await db_session.flush()

        result = await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)

        assert result.baseline_established is True


@pytest.mark.integration
class TestSpecLessItemIsUnextractable:
    """#260: an item with no ``source_specs`` is not extractable, not full-page.

    The API refuses to create one, but the `info.registry` reconcile writes
    whatever an announcement carries and co-core still declares `source_specs`
    optional there — so the state stays reachable over the wire. This guard is
    what makes that residual loud (ERROR health, no revision) instead of silent
    (a whole-page watch nobody configured).
    """

    async def test_spec_less_item_raises_extraction_error(self, db_session):
        wi = await make_watched_item(db_session, name="SpecLessFirstRun")
        wi.effective_url = "https://example.com"
        wi.source_specs = []
        await db_session.flush()

        with pytest.raises(ExtractionError, match="source_specs"):
            await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)

        revs = (
            (
                await db_session.execute(
                    select(ChangeRevision).where(ChangeRevision.watched_item_id == wi.id)
                )
            )
            .scalars()
            .all()
        )
        assert revs == []

    async def test_specs_emptied_after_a_baseline_writes_nothing_and_does_not_notify(
        self, db_session
    ):
        """Unconditional, like #258 — losing the specs is not a content change."""
        wi = await make_watched_item(db_session, name="SpecLessAfterBaseline")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_FULL_PAGE]
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()

        wi.source_specs = []
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            with pytest.raises(ExtractionError):
                await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB)

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
        assert len(revs) == 1  # the baseline only
        assert wi.last_changed_at is None


# ---------------------------------------------------------------------------
# Observation provenance on the outbox row (#253)
# ---------------------------------------------------------------------------


class TestSpecFingerprintOnOutcome:
    """The fingerprint names the spec the loop actually bound, per cannobserv#309.

    Per-spec, not list-level: the fallback moving spec[0] -> spec[1] is a real
    change in what determines the extracted bytes, and Archiver reads the
    position it implies as a selector-rot signal.
    """

    def test_fingerprints_the_spec_actually_used(self):
        outcome = _extract_and_fingerprint(_HTML, [_SPEC_FULL_PAGE])

        assert outcome.spec_fingerprint == spec_fingerprint(_SPEC_FULL_PAGE)
        assert derivation_of(outcome.spec_fingerprint) == "spec1"

    def test_fallback_moves_the_fingerprint_to_the_spec_that_matched(self):
        """spec[0] misses, spec[1] wins — the reported spec must be spec[1]."""
        outcome = _extract_and_fingerprint(_HTML, [_SPEC_MISSES, _SPEC_FULL_PAGE])

        assert outcome.spec_fingerprint == spec_fingerprint(_SPEC_FULL_PAGE)
        assert outcome.spec_fingerprint != spec_fingerprint(_SPEC_MISSES)

    def test_underivable_spec_yields_none_and_does_not_fail_extraction(self):
        """A diagnostic must never fail the pipeline (co-core raises on a float)."""
        spec = {"schema_version": 1, "extraction": {"algorithm": "full_page"}, "weight": 1.5}

        outcome = _extract_and_fingerprint(_HTML, [spec])

        assert outcome.spec_fingerprint is None
        assert outcome.content_size_bytes > 0

    def test_no_source_specs_extracts_nothing_and_names_no_spec(self):
        """#260: the synthetic ``[{}]`` full-page default is gone.

        Nothing is substituted for an absent spec, so there is nothing to
        extract and no spec identity to report. ``spec_fingerprint({})`` would
        have returned a perfectly real value, and that was the problem — it
        names a spec present in no registry, so Archiver's index lookup misses
        and flags the revision as superseded. The caller refuses the outcome
        before it can become a revision.
        """
        outcome = _extract_and_fingerprint(_HTML, [])

        assert outcome.spec_fingerprint is None
        assert outcome.content_size_bytes == 0

    def test_extracted_content_media_type_describes_the_extracted_text(self):
        """Not the source's type — the wire keeps those as separate fields."""
        outcome = _extract_and_fingerprint(_HTML, [_SPEC_FULL_PAGE])

        assert outcome.content_media_type == "text/plain; charset=utf-8"


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
        )

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=blob)
        await db_session.flush()
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=blob)
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
        assert row.spec_fingerprint == spec_fingerprint(_SPEC_FULL_PAGE)
        assert row.content_media_type == "text/plain; charset=utf-8"


# Two full fetches of the same bytes: Replicator re-references the blob on the
# second and publishes a fresh fact with a later horizon (replicator
# docs/STORAGE.md). Same URI, later expiry, new command.
_FIRST_BLOB = BlobProvenance(
    command_id="01J9AAAAAAAAAAAAAAAAAAAAAA",
    blob_uri="gs://co-gcs-blobs/abc",
    source_media_type="text/html",
    blob_expires_at=datetime(2026, 9, 1, tzinfo=UTC),
)
_RENEWED_BLOB = BlobProvenance(
    command_id="01J9BBBBBBBBBBBBBBBBBBBBBB",
    blob_uri="gs://co-gcs-blobs/abc",
    source_media_type="text/html",
    blob_expires_at=datetime(2026, 9, 8, tzinfo=UTC),
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
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_FIRST_BLOB)
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        await db_session.delete(queued)  # the drain published it
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await process_watched_item(
                db_session, wi, raw_content=_HTML_CHANGED, blob=_RENEWED_BLOB
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
        assert row.source_media_type == "text/html"
        assert row.content_media_type == "text/plain; charset=utf-8"
        assert row.spec_fingerprint == spec_fingerprint(_SPEC_FULL_PAGE)
        assert row.dead_lettered_at is None

    async def test_a_baseline_is_never_announced_by_a_cache_hit(self, db_session):
        """The baseline is the one revision the change path never enqueued.
        Announcing it here would be a *first* observation of the pair — a
        registry insert and an ``info.changes`` event — not a renewal."""
        wi = await self._item(db_session)
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_FIRST_BLOB)
        await db_session.flush()

        result = await process_watched_item(db_session, wi, raw_content=_HTML, blob=_RENEWED_BLOB)
        await db_session.flush()

        assert result.cache_hit is True
        assert result.renewal_enqueued is False
        assert await self._outbox_rows(db_session, wi) == []

    async def test_renewal_upserts_a_still_queued_row(self, db_session):
        """``change_revision_id`` is unique: a renewal of a row the drain has not
        published yet updates its provenance in place, never adds a second."""
        wi = await self._item(db_session)
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_FIRST_BLOB)
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        queued_id = queued.id

        result = await process_watched_item(
            db_session, wi, raw_content=_HTML_CHANGED, blob=_RENEWED_BLOB
        )
        await db_session.flush()

        assert result.renewal_enqueued is True
        (row,) = await self._outbox_rows(db_session, wi)
        assert row.id == queued_id
        assert row.command_id == _RENEWED_BLOB.command_id
        assert row.blob_expires_at == _RENEWED_BLOB.blob_expires_at
        assert row.next_attempt_at <= datetime.now(UTC)

    async def test_renewal_revives_a_dead_lettered_row(self, db_session):
        """Dead-lettering is for a payload that is unbuildable *from the row's
        values*; a renewal replaces those values, so the verdict no longer
        applies. The attempt history stays — it is still true."""
        wi = await self._item(db_session)
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_FIRST_BLOB)
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        queued.attempts = 1
        queued.last_error = "unbuildable_payload: ValidationError(...)"
        queued.dead_lettered_at = datetime.now(UTC)
        queued.next_attempt_at = datetime(2099, 1, 1, tzinfo=UTC)
        await db_session.flush()

        result = await process_watched_item(
            db_session, wi, raw_content=_HTML_CHANGED, blob=_RENEWED_BLOB
        )
        await db_session.flush()

        assert result.renewal_enqueued is True
        (row,) = await self._outbox_rows(db_session, wi)
        assert row.dead_lettered_at is None
        assert row.last_error is None
        assert row.next_attempt_at <= datetime.now(UTC)
        assert row.attempts == 1
        assert row.blob_expires_at == _RENEWED_BLOB.blob_expires_at

    async def test_an_unpublishable_reference_never_degrades_a_queued_row(self, db_session):
        """CR 1: the renewal is the only writer that can *overwrite* provenance.

        The change path can only ever create a row, so a wire-required field it
        lacks costs one observation that never existed. Here the same gap would
        replace a publishable row with one the drain dead-letters — a real
        revision lost to a refresh. Unreachable today (``aread_blob`` raises
        before the pipeline on a null URI, and the consumer writes
        ``media_type`` alongside it), so this pins the invariant rather than a
        live path.
        """
        wi = await self._item(db_session)
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_FIRST_BLOB)
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_FIRST_BLOB)
        await db_session.flush()
        (queued,) = await self._outbox_rows(db_session, wi)
        queued_id = queued.id

        typeless = BlobProvenance(
            command_id=_RENEWED_BLOB.command_id,
            blob_uri=_RENEWED_BLOB.blob_uri,
            source_media_type=None,
            blob_expires_at=_RENEWED_BLOB.blob_expires_at,
        )
        result = await process_watched_item(
            db_session, wi, raw_content=_HTML_CHANGED, blob=typeless
        )
        await db_session.flush()

        assert result.cache_hit is True
        assert result.renewal_enqueued is False
        (row,) = await self._outbox_rows(db_session, wi)
        assert row.id == queued_id
        assert row.source_media_type == "text/html"
        assert row.blob_expires_at == _FIRST_BLOB.blob_expires_at

    async def test_an_unpublishable_reference_enqueues_nothing_when_drained(self, db_session):
        """The same guard with no row to protect: a renewal that could only
        dead-letter is not worth queueing."""
        wi = await self._item(db_session)
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_FIRST_BLOB)
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_FIRST_BLOB)
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
        result = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=uriless)
        await db_session.flush()

        assert result.renewal_enqueued is False
        assert await self._outbox_rows(db_session, wi) == []


# ---------------------------------------------------------------------------
# Extractor dispatch (#168 slice 2)
# ---------------------------------------------------------------------------


class _SpyRegistry:
    """Records the essence passed to get_extractor; always returns a real HTML
    extractor so the rest of the pipeline runs on HTML test content."""

    def __init__(self):
        self.essences: list[str | None] = []

    def get_extractor(self, media_type_essence):
        self.essences.append(media_type_essence)
        return HtmlExtractor()


def _make_csv_bytes(rows: int = 5) -> bytes:
    lines = ["name,age"] + [f"person{i},{20 + i}" for i in range(rows)]
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.integration
class TestExtractorDispatch:
    async def _spy_essence(self, db_session, monkeypatch, *, content_media_type, url):
        wi = await make_watched_item(
            db_session, name="Dispatch", content_media_type=content_media_type
        )
        wi.effective_url = url
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()
        spy = _SpyRegistry()
        monkeypatch.setattr("src.workers.pipeline.get_registry", lambda: spy)
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        return spy.essences

    async def test_dispatches_on_content_media_type(self, db_session, monkeypatch):
        essences = await self._spy_essence(
            db_session, monkeypatch, content_media_type="application/pdf", url="https://x.gov/a"
        )
        assert essences == ["application/pdf"]

    async def test_url_extension_tiebreaker(self, db_session, monkeypatch):
        essences = await self._spy_essence(
            db_session, monkeypatch, content_media_type=None, url="https://x.gov/data.csv"
        )
        assert essences == ["text/csv"]

    async def test_ambiguous_header_uses_extension(self, db_session, monkeypatch):
        essences = await self._spy_essence(
            db_session,
            monkeypatch,
            content_media_type="application/octet-stream",
            url="https://x.gov/doc.pdf",
        )
        assert essences == ["application/pdf"]

    async def test_html_default_when_uninformative(self, db_session, monkeypatch):
        essences = await self._spy_essence(
            db_session, monkeypatch, content_media_type=None, url="https://x.gov/page"
        )
        assert essences == [None]

    async def test_uses_injected_registry_not_global(self, db_session):
        """The registry param threads through to extractor dispatch (the injection
        seam) — not the process-global get_registry()."""
        wi = await make_watched_item(
            db_session, name="Injected", content_media_type="application/pdf"
        )
        wi.effective_url = "https://x.gov/a"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()
        spy = _SpyRegistry()
        await process_watched_item(db_session, wi, raw_content=_HTML, registry=spy, blob=_BLOB)
        assert spy.essences == ["application/pdf"]

    async def test_csv_dispatch_changes_fingerprint_vs_html(self, db_session):
        """Real end-to-end: the same CSV bytes fingerprint differently when routed
        to the CsvExcelExtractor (text/csv) vs the HTML fallback (no media type)."""
        csv_bytes = _make_csv_bytes()

        as_csv = await make_watched_item(db_session, name="AsCsv", content_media_type="text/csv")
        as_csv.effective_url = "https://x.gov/data.csv"
        as_csv.source_specs = [{"schema_version": 1}]
        await db_session.flush()
        await process_watched_item(db_session, as_csv, raw_content=csv_bytes, blob=_BLOB)

        as_html = await make_watched_item(db_session, name="AsHtml", content_media_type=None)
        as_html.effective_url = "https://x.gov/data"
        as_html.source_specs = [{"schema_version": 1}]
        await db_session.flush()
        await process_watched_item(db_session, as_html, raw_content=csv_bytes, blob=_BLOB)

        await db_session.flush()
        csv_rev = (
            await db_session.execute(
                select(ChangeRevision).where(ChangeRevision.watched_item_id == as_csv.id)
            )
        ).scalar_one()
        html_rev = (
            await db_session.execute(
                select(ChangeRevision).where(ChangeRevision.watched_item_id == as_html.id)
            )
        ).scalar_one()
        # Both establish a baseline; the CSV row-range extraction differs from the
        # HTML text extraction, so the fingerprints diverge — proof the dispatch ran.
        assert csv_rev.content_fingerprint != html_rev.content_fingerprint


class TestExtractorRegistryWiring:
    """The default registry maps essences to the expected extractor classes."""

    def test_default_registry_maps_media_types(self):
        from src.core.registry import ServiceRegistry

        reg = ServiceRegistry()
        assert isinstance(reg.get_extractor("text/html"), HtmlExtractor)
        assert isinstance(reg.get_extractor("application/pdf"), PdfExtractor)
        assert isinstance(reg.get_extractor("text/csv"), CsvExcelExtractor)
        assert isinstance(reg.get_extractor("application/json"), HtmlExtractor)
