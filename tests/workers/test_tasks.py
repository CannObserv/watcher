"""Tests for check_watch pipeline."""

import pytest

from src.core.models.watch import ContentType, Watch
from src.core.storage import LocalStorage
from src.workers.tasks import _run_check_pipeline

pytestmark = pytest.mark.integration


class TestCheckPipeline:
    """Integration tests for _run_check_pipeline."""

    async def test_first_check_creates_snapshot(self, db_session, tmp_path):
        """First check should create a snapshot and report is_changed=True."""
        watch = Watch(name="Test", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Hello world</p></body></html>"

        result = await _run_check_pipeline(
            watch=watch, raw_content=content, fetcher_used="http",
            fetch_duration_ms=100, storage=storage, session=db_session,
        )
        assert result["snapshot_id"] is not None
        assert result["is_changed"] is True
        assert result["chunk_count"] >= 1

    async def test_identical_content_no_change(self, db_session, tmp_path):
        """Second check with identical content should report is_changed=False."""
        watch = Watch(name="Stable", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Same content</p></body></html>"

        await _run_check_pipeline(
            watch=watch, raw_content=content, fetcher_used="http",
            fetch_duration_ms=100, storage=storage, session=db_session,
        )
        result = await _run_check_pipeline(
            watch=watch, raw_content=content, fetcher_used="http",
            fetch_duration_ms=100, storage=storage, session=db_session,
        )
        assert result["is_changed"] is False

    async def test_different_content_detects_change(self, db_session, tmp_path):
        """Different content on second check should detect a change."""
        watch = Watch(name="Changing", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)

        await _run_check_pipeline(
            watch=watch, raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http", fetch_duration_ms=100, storage=storage, session=db_session,
        )
        result = await _run_check_pipeline(
            watch=watch, raw_content=b"<html><body><p>V2</p></body></html>",
            fetcher_used="http", fetch_duration_ms=100, storage=storage, session=db_session,
        )
        assert result["is_changed"] is True
        assert result["change_id"] is not None

    async def test_stores_raw_content(self, db_session, tmp_path):
        """Pipeline should store raw content retrievable via storage backend."""
        watch = Watch(name="Storage", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Stored</p></body></html>"

        result = await _run_check_pipeline(
            watch=watch, raw_content=content, fetcher_used="http",
            fetch_duration_ms=100, storage=storage, session=db_session,
        )
        stored = storage.load(result["storage_path"])
        assert stored == content
