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
            result = await process_watched_item(db_session, wi, raw_content=_HTML)

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
        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()

        # Same content
        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await process_watched_item(db_session, wi, raw_content=_HTML)

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

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            result = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)

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
        await process_watched_item(db_session, wi, raw_content=_HTML)
        assert wi.last_changed_at is None  # baseline: no change event

        before = datetime.now(UTC)
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)
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

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()
        result = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)
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
        assert syncs[0].content_cache_uri.startswith("file://")

    async def test_scratch_written_on_every_change(self, db_session, tmp_path, monkeypatch):
        """Every change gets a scratch file (consumed by the drain worker)."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        wi = await make_watched_item(db_session, name="WithScratch")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)
        await db_session.flush()

        assert len(list(tmp_path.glob("*.bin"))) == 1

    async def test_dispatches_once_per_watched_item(self, db_session):
        """#191: CHANGE_DETECTED fires exactly once for the WatchedItem (the entity)."""
        wi = await make_watched_item(db_session, name="Item1")
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()

        dispatched_events = []

        async def capture(*, session, event):
            dispatched_events.append(event)

        with patch("src.workers.pipeline.dispatch_event_notifications", side_effect=capture):
            result = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)

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
            await process_watched_item(db_session, wi, raw_content=_HTML)

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

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()

        # Selectors rot: every spec now misses.
        wi.source_specs = [_SPEC_MISSES]
        await db_session.flush()

        with patch(
            "src.workers.pipeline.dispatch_event_notifications", new_callable=AsyncMock
        ) as mock_dispatch:
            with pytest.raises(ExtractionError):
                await process_watched_item(db_session, wi, raw_content=_HTML)

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

        result = await process_watched_item(db_session, wi, raw_content=_HTML)

        assert result.baseline_established is True


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

    async def test_change_without_provenance_still_writes_the_legacy_row(self, db_session):
        """The POST path is untouched in this step — a row with no blob facts
        still drains over HTTP off content_cache_uri."""
        wi = await make_watched_item(db_session, name="NoProvenance")
        wi.effective_url = "https://example.com"
        wi.source_specs = [_SPEC_FULL_PAGE]
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()
        await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)
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
        assert row.content_cache_uri.startswith("file://")
        assert row.blob_uri is None
        assert row.command_id is None


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
        await process_watched_item(db_session, wi, raw_content=_HTML)
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
        await process_watched_item(db_session, wi, raw_content=_HTML, registry=spy)
        assert spy.essences == ["application/pdf"]

    async def test_csv_dispatch_changes_fingerprint_vs_html(self, db_session):
        """Real end-to-end: the same CSV bytes fingerprint differently when routed
        to the CsvExcelExtractor (text/csv) vs the HTML fallback (no media type)."""
        csv_bytes = _make_csv_bytes()

        as_csv = await make_watched_item(db_session, name="AsCsv", content_media_type="text/csv")
        as_csv.effective_url = "https://x.gov/data.csv"
        as_csv.source_specs = [{"schema_version": 1}]
        await db_session.flush()
        await process_watched_item(db_session, as_csv, raw_content=csv_bytes)

        as_html = await make_watched_item(db_session, name="AsHtml", content_media_type=None)
        as_html.effective_url = "https://x.gov/data"
        as_html.source_specs = [{"schema_version": 1}]
        await db_session.flush()
        await process_watched_item(db_session, as_html, raw_content=csv_bytes)

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
