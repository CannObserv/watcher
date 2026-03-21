"""Tests for check_watch pipeline and task wrappers."""

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.fetchers.http import HttpFetcher
from src.core.models.audit_log import AuditLog
from src.core.models.watch import ContentType, Watch
from src.core.rate_limiter import DomainRateLimiter
from src.core.storage import LocalStorage
from src.workers.tasks import _run_check_pipeline, check_watch

pytestmark = pytest.mark.integration


def _mock_session_factory(db_session: AsyncSession):
    """Create a mock session factory that returns the test session.

    check_watch creates its own session via get_session_factory()().
    This patches it to use the test DB session instead.
    """

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


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


class TestCheckWatchTask:
    """Tests for the check_watch procrastinate task wrapper.

    Uses monkeypatch to inject test DB session via get_session_factory.
    """

    async def test_429_reports_rate_limit(self, db_session, tmp_path, monkeypatch):
        """A 429 response should report rate limiting and raise ConnectionError."""
        import src.workers.tasks as tasks_mod

        watch = Watch(
            name="Rate Limited",
            url="https://example.com/limited",
            content_type=ContentType.HTML,
        )
        db_session.add(watch)
        await db_session.flush()

        mock_response = httpx.Response(
            429, content=b"Too Many Requests",
            request=httpx.Request("GET", "https://example.com/limited"),
        )
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: mock_response)
        )

        monkeypatch.setattr(tasks_mod, "_fetcher", HttpFetcher(client=mock_client))
        monkeypatch.setattr(tasks_mod, "_rate_limiter", DomainRateLimiter(min_interval=0.0))
        monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        with pytest.raises(ConnectionError, match="Rate limited"):
            await check_watch(str(watch.id))

    async def test_inactive_watch_skipped(self, db_session, tmp_path, monkeypatch):
        """Inactive watches should be skipped without fetching."""
        import src.workers.tasks as tasks_mod

        watch = Watch(
            name="Inactive",
            url="https://example.com/inactive",
            content_type=ContentType.HTML,
            is_active=False,
        )
        db_session.add(watch)
        await db_session.flush()

        monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await check_watch(str(watch.id))
        assert result.get("skipped") is True

    async def test_fetch_failure_logs_audit(self, db_session, tmp_path, monkeypatch):
        """Non-success HTTP status should log audit and return error."""
        import src.workers.tasks as tasks_mod

        watch = Watch(
            name="Server Error",
            url="https://example.com/error",
            content_type=ContentType.HTML,
        )
        db_session.add(watch)
        await db_session.flush()

        mock_response = httpx.Response(
            500, content=b"Internal Server Error",
            request=httpx.Request("GET", "https://example.com/error"),
        )
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: mock_response)
        )

        monkeypatch.setattr(tasks_mod, "_fetcher", HttpFetcher(client=mock_client))
        monkeypatch.setattr(tasks_mod, "_rate_limiter", DomainRateLimiter(min_interval=0.0))
        monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await check_watch(str(watch.id))
        assert "error" in result

        # Verify audit log entry was written
        stmt = select(AuditLog).where(AuditLog.event_type == "check.fetch_failed")
        audit_result = await db_session.execute(stmt)
        entries = audit_result.scalars().all()
        assert len(entries) >= 1
        assert entries[0].payload["status_code"] == 500
