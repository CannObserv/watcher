"""Tests for pipeline helpers and process_watched_item.

Unit tests: _extraction_config_from_spec, _extract_with_spec.
Integration tests: process_watched_item baseline + change detection paths.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.workers.pipeline import (
    WatchedItemResult,
    _extract_with_spec,
    _extraction_config_from_spec,
    process_watched_item,
)
from tests.conftest import make_watch, make_watched_item


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


@pytest.mark.asyncio
class TestExtractWithSpec:
    async def test_extracts_html_with_full_page_algorithm(self):
        document = {"extraction": {"algorithm": "full_page"}}
        result = await _extract_with_spec(b"<html><body><p>Hello</p></body></html>", document)
        assert len(result.chunks) >= 1
        assert any("Hello" in c.text for c in result.chunks)

    async def test_css_selector_filters_to_matching_section(self):
        document = {"extraction": {"algorithm": "css", "selector": ".target"}}
        result = await _extract_with_spec(
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
        watch = await make_watch(db_session, name="Baseline")
        wi = watch.watched_item
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
        watch = await make_watch(db_session, name="CacheHit")
        wi = watch.watched_item
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
        """Content change: new ChangeRevision + CHANGE_DETECTED per active Watch."""
        watch = await make_watch(db_session, name="Changed")
        wi = watch.watched_item
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
        assert event.watched_item_id == str(watch.id)
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
        watch = await make_watch(db_session, name="Timestamps")
        wi = watch.watched_item
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

    async def test_no_archiver_sync_when_archiver_info_source_id_not_set(self, db_session):
        """No PendingArchiverSync inserted when archiver_info_source_id is NULL."""
        watch = await make_watch(db_session, name="NoSync")
        wi = watch.watched_item
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        assert wi.archiver_info_source_id is None
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()
        result = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)
        await db_session.flush()

        assert result.archiver_sync_enqueued is False
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

    async def test_archiver_sync_enqueued_when_archiver_id_set(self, db_session):
        """PendingArchiverSync inserted when archiver_info_source_id is set + content changed."""
        from src.core.models.base import generate_ulid

        watch = await make_watch(db_session, name="WithSync")
        wi = watch.watched_item
        wi.effective_url = "https://example.com"
        wi.source_specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        wi.archiver_info_source_id = str(generate_ulid())
        await db_session.flush()

        await process_watched_item(db_session, wi, raw_content=_HTML)
        await db_session.flush()
        result = await process_watched_item(db_session, wi, raw_content=_HTML_CHANGED)
        await db_session.flush()

        assert result.archiver_sync_enqueued is True
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
