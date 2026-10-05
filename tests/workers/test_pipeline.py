"""Tests for pipeline helpers and process_watched_item.

Unit tests: _extraction_config_from_spec, _extract_with_spec.
Integration tests: process_watched_item baseline + change detection paths.
"""

import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from co_core.pure.extract import (
    CANONICAL_TEXT_MEDIA_TYPE,
    canonical_text,
    canonical_text_fingerprint,
    derivation_of,
    processor_version,
    spec_fingerprint,
    spec_schema_version,
)
from co_core.pure.extract.html import HtmlExtractor
from notifier_client.types import DispatchOutStatus
from sqlalchemy import select
from ulid import ULID

from src.core.fetch_commands import create_fetch_command
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.change_revision import ChangeRevision
from src.core.models.notification_template import VISIBILITY_GLOBAL, NotificationTemplate
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.process_command import LocalOutcome, ProcessCommandStatus
from src.core.process_commands import LocalExtraction, create_process_command
from src.core.validators import EXTRACTION_GENERATION, LOCAL_EXTRACTION_GENERATION
from src.workers.pipeline import (
    BlobProvenance,
    ExtractionError,
    ExtractionOutcome,
    WatchedItemResult,
    _extract_and_fingerprint,
    _extract_with_spec,
    _extraction_config_from_spec,
    apply_extraction_outcome,
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

        async def capture(*, session, event, current_text=None):
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


class TestCanonicalTextAdoption:
    """#324: the fingerprint's bytes are co-core's `canonical_text`, not a local join.

    The derived text is about to be stored permanently by hash and compared
    across two services (cannobserv#486), so the bytes the hash covers are
    defined once, in co-core. Every stored ``content_fingerprint`` must already
    equal ``canonical_text_fingerprint`` of the same chunks — the equality the
    design's diff and shadow comparator both stand on.
    """

    def test_fingerprint_is_canonical_text_fingerprint_of_the_chunks(self):
        chunks = _extract_with_spec(_HTML, _SPEC_FULL_PAGE).chunks
        outcome = _extract_and_fingerprint(_HTML, [_SPEC_FULL_PAGE])
        assert outcome.content_fingerprint == canonical_text_fingerprint(chunks)
        assert outcome.content_size_bytes == len(canonical_text(chunks))

    def test_bytes_equal_the_join_they_replaced(self):
        # The pre-#324 expression, verbatim: every stored fingerprint was
        # computed over it, so equality here is what keeps them valid.
        chunks = _extract_with_spec(_HTML, _SPEC_FULL_PAGE).chunks
        assert canonical_text(chunks) == "\n".join(c.text for c in chunks).encode()
        outcome = _extract_and_fingerprint(_HTML, [_SPEC_FULL_PAGE])
        assert outcome.content_fingerprint == (
            "sha256:" + hashlib.sha256("\n".join(c.text for c in chunks).encode()).hexdigest()
        )

    def test_outcome_reports_the_processor_version(self):
        # Spelled through co-core's helper so Observo's fact and watcher's local
        # extraction agree character for character (design Section 5, shadow).
        outcome = _extract_and_fingerprint(_HTML, [_SPEC_FULL_PAGE])
        assert outcome.processor_version == processor_version(LOCAL_EXTRACTION_GENERATION)
        assert outcome.processor_version == EXTRACTION_GENERATION

    def test_content_media_type_is_the_canonical_constant(self):
        outcome = _extract_and_fingerprint(_HTML, [_SPEC_FULL_PAGE])
        assert outcome.content_media_type == CANONICAL_TEXT_MEDIA_TYPE

    def test_schema_version_is_derived_by_co_core(self):
        # `spec_schema_version` coerces a digit string the way the local
        # `int(...)` always did, and rejects a bool where `int(True)` silently
        # read 1 — the processor and the issuer must not default differently.
        spec = {"schema_version": "2", "extraction": {"algorithm": "full_page"}}
        assert _extract_and_fingerprint(_HTML, [spec]).schema_version == 2
        assert _extract_and_fingerprint(_HTML, [spec]).schema_version == spec_schema_version(spec)
        malformed = {"schema_version": True, "extraction": {"algorithm": "full_page"}}
        with pytest.raises(ValueError):
            _extract_and_fingerprint(_HTML, [malformed])


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
        wi = await make_watched_item(db_session, name="Identity baseline")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_FULL_PAGE]
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()

        (baseline,) = await self._revisions(db_session, wi)
        assert baseline.spec_fingerprint == spec_fingerprint(_SPEC_FULL_PAGE)
        assert baseline.processor_version == EXTRACTION_GENERATION

    async def test_underivable_spec_stores_null_not_a_lost_revision(self, db_session):
        # co-core rejects a float in a spec; the identity is a diagnostic, so the
        # revision is still written — with NULL, which Option A reads as unknown.
        spec = {"schema_version": 1, "extraction": {"algorithm": "full_page"}, "weight": 1.5}
        wi = await make_watched_item(db_session, name="Identity underivable")
        wi.effective_url = "https://example.com"
        wi.source_specs = [spec]
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()

        (baseline,) = await self._revisions(db_session, wi)
        assert baseline.spec_fingerprint is None
        assert baseline.processor_version == EXTRACTION_GENERATION

    async def test_change_carries_the_spec_that_matched(self, db_session):
        wi = await make_watched_item(db_session, name="Identity change")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_MISSES, _SPEC_FULL_PAGE]
        await db_session.flush()

        with patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock):
            await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
            await db_session.flush()
            await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB)
            await db_session.flush()

        _baseline, change = await self._revisions(db_session, wi)
        assert change.spec_fingerprint == spec_fingerprint(_SPEC_FULL_PAGE)
        assert change.processor_version == EXTRACTION_GENERATION


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
        assert row.blob_fingerprint == blob.blob_fingerprint
        assert row.spec_fingerprint == spec_fingerprint(_SPEC_FULL_PAGE)
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
        assert row.blob_fingerprint == _RENEWED_BLOB.blob_fingerprint
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
        assert row.blob_fingerprint == _RENEWED_BLOB.blob_fingerprint
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
        assert row.blob_fingerprint == _RENEWED_BLOB.blob_fingerprint

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
        assert row.blob_fingerprint == _FIRST_BLOB.blob_fingerprint  # the pair stays whole

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


@pytest.mark.integration
class TestResultCarriesTheLocalAnswer:
    """The result names what local extraction concluded (#325).

    Shadow mode's comparator judges the processor's ``output_digest`` against
    it, so every branch that extracted — baseline, both cache-hit returns, and
    change — reports the fingerprint it computed and the spec that bound.
    """

    SPEC = {"schema_version": 1, "extraction": {"algorithm": "full_page"}}

    async def _item(self, db_session, name):
        wi = await make_watched_item(db_session, name=name, source_specs=[self.SPEC])
        await db_session.flush()
        return wi

    async def _latest_fingerprint(self, db_session, wi):
        return (
            await db_session.execute(
                select(ChangeRevision.content_fingerprint)
                .where(ChangeRevision.watched_item_id == wi.id)
                .order_by(ChangeRevision.captured_at.desc())
                .limit(1)
            )
        ).scalar_one()

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_every_branch_reports_fingerprint_and_spec(self, _dispatch, db_session):
        wi = await self._item(db_session, "LocalAnswer")
        expected_spec = spec_fingerprint(self.SPEC)

        baseline = await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()
        assert baseline.content_fingerprint == await self._latest_fingerprint(db_session, wi)
        assert baseline.spec_fingerprint == expected_spec

        unannounced_hit = await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        assert unannounced_hit.cache_hit is True
        assert unannounced_hit.content_fingerprint == baseline.content_fingerprint
        assert unannounced_hit.spec_fingerprint == expected_spec

        changed = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB)
        await db_session.flush()
        assert changed.changed is True
        assert changed.content_fingerprint == await self._latest_fingerprint(db_session, wi)
        assert changed.content_fingerprint != baseline.content_fingerprint
        assert changed.spec_fingerprint == expected_spec

        announced_hit = await process_watched_item(
            db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB
        )
        assert announced_hit.cache_hit is True
        assert announced_hit.content_fingerprint == changed.content_fingerprint
        assert announced_hit.spec_fingerprint == expected_spec


@pytest.mark.integration
class TestChangeEventCarriesTheDiffAddresses:
    """#222: a change names both texts by their storage address — the
    fingerprint *is* where the processor keeps the canonical text — and local
    extraction hands the current text it already holds to dispatch, because the
    processor has not answered for it yet."""

    async def _changed(self, db_session, name):
        wi = await make_watched_item(db_session, name=name, source_specs=[_SPEC_FULL_PAGE])
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        await db_session.flush()
        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as dispatch:
            await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB)
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

    async def test_local_extraction_hands_over_the_current_text(self, db_session):
        dispatch, _baseline, change = await self._changed(db_session, "Diff in hand")
        current = dispatch.call_args.kwargs["current_text"]
        assert f"sha256:{hashlib.sha256(current).hexdigest()}" == change.content_fingerprint

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_derived_outcome_has_no_text_in_hand(self, dispatch, db_session):
        wi = await make_watched_item(
            db_session, name="Diff derived", source_specs=[_SPEC_FULL_PAGE]
        )
        for fp in ("sha256:" + "aa" * 32, "sha256:" + "bb" * 32):
            outcome = ExtractionOutcome(
                content_fingerprint=fp, content_size_bytes=10, schema_version=1
            )
            await apply_extraction_outcome(db_session, wi, outcome, blob=_BLOB)
            await db_session.flush()
        assert dispatch.call_args.kwargs["current_text"] is None


async def _audits_of(db_session, event_type, wi) -> list[AuditLog]:
    stmt = select(AuditLog).where(
        AuditLog.event_type == event_type,
        AuditLog.payload["watched_item_id"].astext == str(wi.id),
    )
    return list((await db_session.execute(stmt)).scalars())


class TestChangeDiffEndToEnd:
    """#222 acceptance: a change with Full diff enabled delivers the diff.

    Nothing between the pipeline and the notifier is mocked: the real
    dispatcher selects the template, the real loader finds the previous text
    through its ``process_commands`` row (under its savepoint) and takes the
    current text from the pipeline's hand, and the real renderer fences the
    diff. Only the two edges are stubbed — the notifier client and the GCS read.
    """

    async def test_full_diff_reaches_the_notifier(self, db_session):
        wi = await make_watched_item(db_session, name="Diff e2e", source_specs=[_SPEC_FULL_PAGE])
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        previous = _extract_and_fingerprint(_HTML, [_SPEC_FULL_PAGE]).content
        uri = f"gs://co-gcs-processor/blobs/{hashlib.sha256(previous).hexdigest()}.bin"

        fetch = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        fetch.blob_uri, fetch.content_fingerprint = _BLOB.blob_uri, "61" * 32
        await db_session.flush()
        answered = await create_process_command(
            db_session,
            fetch,
            wi,
            now=datetime.now(UTC),
            local=LocalExtraction(outcome=LocalOutcome.BASELINE),
        )
        answered.status = ProcessCommandStatus.COMPLETED
        answered.output_digest = f"sha256:{hashlib.sha256(previous).hexdigest()}"
        answered.output_uri = uri
        answered.output_size_bytes = len(previous)
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
                new=AsyncMock(side_effect=lambda u: {uri: previous}[u]),
            ),
        ):
            result = await process_watched_item(
                db_session, wi, raw_content=_HTML_CHANGED, blob=_BLOB
            )
        await db_session.flush()

        assert result.changed is True
        body = client.dispatch.call_args.kwargs["body_template"]
        fenced = body.split("\n\n", 1)[1]
        assert fenced.startswith("```diff\n--- previous\n+++ current\n")
        assert "-Hello world" in fenced
        assert "+Content changed" in fenced
        assert "DIFF: unavailable" not in body
        (dispatched,) = await _audits_of(db_session, EventType.NOTIFICATION_DISPATCHED, wi)
        assert dispatched.payload["results"][0]["success"] is True


class TestOptionA:
    """D6: a fingerprint move the extractor caused is not a content change (#326).

    The outcome is applied as the processor reports it (``apply_extraction_
    outcome``), so these tests hand it the identity fields directly. Spec
    identity is compared against the previous *revision*; processor identity
    against ``WatchedItem.processor_version`` read before the outcome moves it
    — an equal digest under a new version refreshes the item, never the
    revision, so the revision's own version can be stale.
    """

    SPEC_FP = "spec1:sha256:" + "11" * 32
    OTHER_SPEC_FP = "spec1:sha256:" + "22" * 32
    BASE_FP = "sha256:" + "aa" * 32
    NEXT_FP = "sha256:" + "bb" * 32

    def _outcome(self, fingerprint, *, spec=SPEC_FP, version="0.19.7+1"):
        return ExtractionOutcome(
            content_fingerprint=fingerprint,
            content_size_bytes=10,
            schema_version=1,
            spec_fingerprint=spec,
            processor_version=version,
        )

    async def _baselined(self, db_session, name, **outcome_kwargs):
        wi = await make_watched_item(db_session, name=name, source_specs=[_SPEC_FULL_PAGE])
        await db_session.flush()
        await apply_extraction_outcome(
            db_session, wi, self._outcome(self.BASE_FP, **outcome_kwargs), blob=_BLOB
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

    async def test_local_extraction_records_its_own_generation(self, db_session):
        wi = await make_watched_item(
            db_session, name="OptionA local", source_specs=[_SPEC_FULL_PAGE]
        )
        await db_session.flush()
        await process_watched_item(db_session, wi, raw_content=_HTML, blob=_BLOB)
        assert wi.processor_version == EXTRACTION_GENERATION

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_processor_change_alone_re_baselines_silently(self, dispatch, db_session):
        wi = await self._baselined(db_session, "OptionA processor")
        before = wi.last_changed_at

        result = await apply_extraction_outcome(
            db_session, wi, self._outcome(self.NEXT_FP, version="0.20.0+1"), blob=_BLOB
        )
        await db_session.flush()

        assert result.rebaselined is True
        assert result.changed is False
        assert result.notifications_dispatched == 0
        dispatch.assert_not_awaited()
        _baseline, rebaseline = await self._revisions(db_session, wi)
        assert rebaseline.content_fingerprint == self.NEXT_FP
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
            db_session, wi, self._outcome(self.NEXT_FP, version="0.20.0+1"), blob=_BLOB
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
            db_session, wi, self._outcome(self.NEXT_FP, spec=self.OTHER_SPEC_FP), blob=_BLOB
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
            self._outcome(self.NEXT_FP, spec=self.OTHER_SPEC_FP, version="0.20.0+1"),
            blob=_BLOB,
        )

        assert result.changed is True
        assert dispatch.call_args.kwargs["event"].metadata["extraction_changed"] == "spec"
        assert await self._rebaselined_audits(db_session, wi) == []

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_a_content_change_carries_no_label(self, dispatch, db_session):
        wi = await self._baselined(db_session, "OptionA content")

        result = await apply_extraction_outcome(
            db_session, wi, self._outcome(self.NEXT_FP), blob=_BLOB
        )

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
            self._outcome(self.NEXT_FP, spec=self.OTHER_SPEC_FP, version="0.20.0+1"),
            blob=_BLOB,
        )

        assert result.changed is True
        assert dispatch.call_args.kwargs["event"].metadata["extraction_changed"] is None

    @patch("src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock)
    async def test_an_equal_digest_refreshes_the_items_version_only(self, dispatch, db_session):
        wi = await self._baselined(db_session, "OptionA refresh")

        result = await apply_extraction_outcome(
            db_session, wi, self._outcome(self.BASE_FP, version="0.20.0+1"), blob=_BLOB
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
            db_session, wi, self._outcome(self.BASE_FP, version="0.20.0+1"), blob=_BLOB
        )
        await db_session.flush()

        result = await apply_extraction_outcome(
            db_session, wi, self._outcome(self.NEXT_FP, version="0.20.0+1"), blob=_BLOB
        )

        assert result.changed is True
        assert result.rebaselined is False
        dispatch.assert_awaited_once()
