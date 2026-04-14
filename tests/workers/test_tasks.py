"""Tests for check_watch pipeline and task wrappers."""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import src.workers.tasks as tasks_mod
from src.core.crypto import encrypt_apprise_url
from src.core.fetchers.http import HttpFetcher
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watch import ContentType, Watch, WatchHealthStatus
from src.core.notifications.events import WatchEventType
from src.core.rate_limiter import DomainRateLimiter
from src.core.registry import ServiceRegistry
from src.core.storage import LocalStorage
from src.workers.pipeline import _maybe_decay_backoff
from src.workers.tasks import (
    _persist_backoff,
    _run_check_pipeline,
    _watch_base_metadata,
    check_watch,
    schedule_tick,
)

pytestmark = pytest.mark.integration


class TestWatchBaseMetadata:
    """Unit tests for _watch_base_metadata helper (no DB required)."""

    def _make_watch(self, **kwargs):
        return Watch(
            name="Test", url="https://example.com", content_type=ContentType.HTML, **kwargs
        )

    def test_includes_last_changed_at_when_set(self):
        watch = self._make_watch(last_changed_at=datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC))
        meta = _watch_base_metadata(watch)
        assert meta["last_changed_at"] == "2026-04-09"

    def test_omits_last_changed_at_when_none(self):
        watch = self._make_watch(last_changed_at=None)
        meta = _watch_base_metadata(watch)
        assert "last_changed_at" not in meta

    def test_includes_effective_domain_when_set(self):
        watch = self._make_watch(effective_domain="example.com")
        meta = _watch_base_metadata(watch)
        assert meta["effective_domain"] == "example.com"

    def test_includes_check_interval_when_set(self):
        watch = self._make_watch(schedule_config={"interval": "1h"})
        meta = _watch_base_metadata(watch)
        assert meta["check_interval"] == "1h"


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


class TestPersistBackoff:
    async def test_persist_backoff_updates_domain(self):
        domain = MagicMock()
        domain.current_interval = 1.0
        domain.last_request_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        before = datetime.now(UTC)
        await _persist_backoff("example.com", 4.0, mock_session)

        assert domain.current_interval == 4.0
        assert domain.last_request_at >= before

    async def test_persist_backoff_noop_if_domain_missing(self):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Should not raise
        await _persist_backoff("unknown.com", 4.0, mock_session)


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
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )
        assert result["is_changed"] is False

    async def test_different_content_detects_change(self, db_session, tmp_path):
        """Different content on second check should detect a change."""
        watch = Watch(name="Changing", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)

        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
            429,
            content=b"Too Many Requests",
            request=httpx.Request("GET", "https://example.com/limited"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))

        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(fetcher=HttpFetcher(client=mock_client))
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
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
            500,
            content=b"Internal Server Error",
            request=httpx.Request("GET", "https://example.com/error"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))

        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(fetcher=HttpFetcher(client=mock_client))
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
        monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await check_watch(str(watch.id))
        assert "error" in result

        # Verify audit log entry was written
        stmt = select(AuditLog).where(AuditLog.event_type == EventType.CHECK_FETCH_FAILED)
        audit_result = await db_session.execute(stmt)
        entries = audit_result.scalars().all()
        assert len(entries) >= 1
        assert entries[0].payload["status_code"] == 500


class TestCheckWatchSavepointBoundary:
    """Integration tests for savepoint boundary: pipeline commits before notifications."""

    async def test_pipeline_committed_before_notifications(self, db_session, tmp_path, monkeypatch):
        """Snapshot/change records must be committed before notification dispatch.

        Verifies that check_watch calls session.commit() after _run_check_pipeline()
        and before dispatch_notifications(), ensuring pipeline results survive a
        notification failure.
        """
        watch = Watch(
            name="Savepoint Test",
            url="https://example.com/savepoint",
            content_type=ContentType.HTML,
        )
        db_session.add(watch)
        await db_session.flush()

        # Add an active notification config so dispatch is triggered
        nc = WatchNotificationConfig(
            watch_id=watch.id,
            apprise_url=encrypt_apprise_url("json://localhost/notify"),
            channel_hint="json",
            events=["change_detected"],
            is_active=True,
        )
        db_session.add(nc)
        await db_session.commit()

        # First check to establish a baseline snapshot (no change_id on first check)
        storage = LocalStorage(base_dir=tmp_path)
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>Original</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=50,
            storage=storage,
            session=db_session,
        )
        await db_session.commit()

        # Second check with changed content triggers change detection → dispatch
        mock_response = httpx.Response(
            200,
            content=b"<html><body><p>Changed content</p></body></html>",
            request=httpx.Request("GET", "https://example.com/savepoint"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))

        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(fetcher=HttpFetcher(client=mock_client))
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
        monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        commit_calls: list[str] = []
        original_commit = db_session.commit

        async def tracking_commit():
            commit_calls.append("commit")
            await original_commit()

        monkeypatch.setattr(db_session, "commit", tracking_commit)

        # dispatch_notifications records the commit count at call time
        dispatch_call_index: list[int] = []

        async def mock_dispatch(session, event):
            dispatch_call_index.append(len(commit_calls))

        monkeypatch.setattr(tasks_mod, "dispatch_event_notifications", mock_dispatch)

        await check_watch(str(watch.id))

        # dispatch must have been called after at least one commit (the pipeline commit)
        assert len(dispatch_call_index) == 1, "dispatch_notifications should be called once"
        assert dispatch_call_index[0] >= 1, (
            "dispatch_notifications must be called after at least one session.commit()"
        )


class TestScheduleTickWithProfiles:
    """Integration tests for schedule_tick temporal profile awareness."""

    async def test_profile_accelerates_check_interval(self, db_session, monkeypatch):
        """A watch with a temporal profile should be deferred sooner than its base interval."""
        import src.workers.tasks as tasks_mod

        # Watch with 1-day base interval, last checked 2 hours ago
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        watch = Watch(
            name="Profiled",
            url="https://example.com/agenda",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=2),
        )
        db_session.add(watch)
        await db_session.flush()

        # Event profile: 7 days before April 15 → 1h interval
        profile = TemporalProfile(
            watch_id=watch.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 15),
            rules=[{"days_before": 7, "interval": "1h"}],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        db_session.add(profile)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        # Mock check_watch.configure().defer_async to capture calls
        defer_calls = []
        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock(
            side_effect=lambda **kw: defer_calls.append(kw)
        )
        monkeypatch.setattr(check_watch, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        # Without profile: 1d interval, last checked 2h ago → not due
        # With profile: 1h interval, last checked 2h ago → overdue → should defer
        assert len(defer_calls) == 1
        assert defer_calls[0]["watch_id"] == str(watch.id)

    async def test_post_action_deactivates_watch(self, db_session, monkeypatch):
        """A watch with an expired event profile and deactivate action should be deactivated."""
        import src.workers.tasks as tasks_mod

        # Event was April 5, today is April 10 → past
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        watch = Watch(
            name="Expired Event",
            url="https://example.com/past-event",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=25),
        )
        db_session.add(watch)
        await db_session.flush()

        profile = TemporalProfile(
            watch_id=watch.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 5),
            rules=[{"days_before": 7, "interval": "1h"}],
            post_action=PostAction.DEACTIVATE,
        )
        db_session.add(profile)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock()
        monkeypatch.setattr(check_watch, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        # Watch should be deactivated, no check deferred
        await db_session.refresh(watch)
        assert watch.is_active is False
        mock_configure.return_value.defer_async.assert_not_called()

        # Profile should be deactivated
        await db_session.refresh(profile)
        assert profile.is_active is False

    async def test_post_action_archive_sets_is_archived(self, db_session, monkeypatch):
        """Archive post-action sets both is_active=False and is_archived=True."""
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        watch = Watch(
            name="Archive Event",
            url="https://example.com/archive-event",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=25),
        )
        db_session.add(watch)
        await db_session.flush()

        profile = TemporalProfile(
            watch_id=watch.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 5),
            rules=[{"days_before": 7, "interval": "1h"}],
            post_action=PostAction.ARCHIVE,
        )
        db_session.add(profile)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock()
        monkeypatch.setattr(check_watch, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        await db_session.refresh(watch)
        assert watch.is_active is False
        assert watch.is_archived is True
        mock_configure.return_value.defer_async.assert_not_called()

    async def test_post_action_deactivate_does_not_set_is_archived(self, db_session, monkeypatch):
        """Deactivate post-action sets is_active=False but leaves is_archived=False."""
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        watch = Watch(
            name="Deactivate Event",
            url="https://example.com/deact-event",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=25),
        )
        db_session.add(watch)
        await db_session.flush()

        profile = TemporalProfile(
            watch_id=watch.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 5),
            rules=[{"days_before": 7, "interval": "1h"}],
            post_action=PostAction.DEACTIVATE,
        )
        db_session.add(profile)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock()
        monkeypatch.setattr(check_watch, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        await db_session.refresh(watch)
        assert watch.is_active is False
        assert watch.is_archived is False


class TestMaybeDecayBackoff:
    async def test_resets_when_decay_window_exceeded(self):
        domain = MagicMock()
        domain.name = "example.com"
        domain.min_interval = 1.0
        domain.current_interval = 8.0
        domain.decay_window = 1800.0
        domain.last_request_at = datetime.now(UTC) - timedelta(seconds=1801)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()
        limiter.configure_domain(
            "example.com", max_concurrency=2, min_interval=1.0, current_interval=8.0
        )

        decayed = await _maybe_decay_backoff("example.com", limiter, mock_session)
        assert decayed is True
        assert domain.current_interval == 1.0
        assert domain.last_request_at is None
        assert limiter._domains["example.com"].current_interval == 1.0

    async def test_no_reset_when_within_decay_window(self):
        domain = MagicMock()
        domain.name = "example.com"
        domain.min_interval = 1.0
        domain.current_interval = 8.0
        domain.decay_window = 1800.0
        domain.last_request_at = datetime.now(UTC) - timedelta(seconds=600)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()
        limiter.configure_domain(
            "example.com", max_concurrency=2, min_interval=1.0, current_interval=8.0
        )

        decayed = await _maybe_decay_backoff("example.com", limiter, mock_session)
        assert decayed is False
        assert limiter._domains["example.com"].current_interval == 8.0

    async def test_noop_when_not_in_backoff(self):
        domain = MagicMock()
        domain.name = "example.com"
        domain.min_interval = 1.0
        domain.current_interval = 1.0
        domain.decay_window = 1800.0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()
        decayed = await _maybe_decay_backoff("example.com", limiter, mock_session)
        assert decayed is False

    async def test_noop_when_domain_not_found(self):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()
        decayed = await _maybe_decay_backoff("unknown.com", limiter, mock_session)
        assert decayed is False


class TestScheduleTickInactiveDomain:
    """schedule_tick must not defer checks for watches on inactive domains."""

    async def test_skips_watches_on_inactive_domain(self, db_session, monkeypatch):
        import src.workers.tasks as tasks_mod

        domain = Domain(name="paused.com", is_active=False)
        db_session.add(domain)
        watch = Watch(
            name="On Paused Domain",
            url="https://paused.com/p",
            content_type=ContentType.HTML,
            effective_domain="paused.com",
            is_active=True,
        )
        db_session.add(watch)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        defer_calls = []
        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock(
            side_effect=lambda **kw: defer_calls.append(kw)
        )
        monkeypatch.setattr(check_watch, "configure", mock_configure)

        await schedule_tick(0)

        assert defer_calls == [], "should not defer check for watch on inactive domain"

    async def test_defers_watches_on_active_domain(self, db_session, monkeypatch):
        import src.workers.tasks as tasks_mod

        domain = Domain(name="active-ctrl.com", is_active=True)
        db_session.add(domain)
        watch = Watch(
            name="On Active Domain",
            url="https://active-ctrl.com/p",
            content_type=ContentType.HTML,
            effective_domain="active-ctrl.com",
            is_active=True,
        )
        db_session.add(watch)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        defer_calls = []
        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock(
            side_effect=lambda **kw: defer_calls.append(kw)
        )
        monkeypatch.setattr(check_watch, "configure", mock_configure)

        await schedule_tick(0)

        assert len(defer_calls) == 1
        assert defer_calls[0]["watch_id"] == str(watch.id)


class TestCheckWatchInactiveDomain:
    """check_watch must skip if the watch's domain is inactive."""

    async def test_skips_when_domain_inactive(self, db_session, tmp_path, monkeypatch):
        import src.workers.tasks as tasks_mod

        domain = Domain(name="skipped.com", is_active=False)
        db_session.add(domain)
        watch = Watch(
            name="Domain Inactive Watch",
            url="https://skipped.com/p",
            content_type=ContentType.HTML,
            effective_domain="skipped.com",
            is_active=True,
        )
        db_session.add(watch)
        await db_session.commit()

        monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await check_watch(str(watch.id))
        assert result.get("skipped") is True


class TestCheckWatchHealthTransitions:
    """Test watch_error and watch_recovered state transition events."""

    async def test_fetch_failure_sets_health_error_and_emits_watch_error(
        self, db_session, monkeypatch
    ):
        """First fetch failure transitions health_status to ERROR and notifies."""
        watch = Watch(
            name="Health Test",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.OK,
        )
        db_session.add(watch)
        await db_session.commit()
        await db_session.refresh(watch)

        mock_fetch_result = MagicMock()
        mock_fetch_result.is_success = False
        mock_fetch_result.status_code = 503
        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=mock_fetch_result)

        dispatched_events = []

        async def fake_dispatch(session, event):
            dispatched_events.append(event)

        monkeypatch.setattr("src.workers.tasks.dispatch_event_notifications", fake_dispatch)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        reg = ServiceRegistry(fetcher=mock_fetcher)
        await check_watch(str(watch.id), registry=reg)

        await db_session.refresh(watch)
        assert watch.health_status == WatchHealthStatus.ERROR
        assert any(e.event_type == WatchEventType.WATCH_ERROR for e in dispatched_events)

    async def test_repeated_failure_does_not_emit_watch_error_again(self, db_session, monkeypatch):
        """Repeated failures after first do NOT re-emit watch_error."""
        watch = Watch(
            name="Already Error",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.ERROR,
        )
        db_session.add(watch)
        await db_session.commit()
        await db_session.refresh(watch)

        mock_fetch_result = MagicMock()
        mock_fetch_result.is_success = False
        mock_fetch_result.status_code = 503
        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=mock_fetch_result)

        dispatched_events = []

        async def fake_dispatch(session, event):
            dispatched_events.append(event)

        monkeypatch.setattr("src.workers.tasks.dispatch_event_notifications", fake_dispatch)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        reg = ServiceRegistry(fetcher=mock_fetcher)
        await check_watch(str(watch.id), registry=reg)

        assert not any(e.event_type == WatchEventType.WATCH_ERROR for e in dispatched_events)

    async def test_recovery_emits_watch_recovered(self, db_session, monkeypatch, tmp_path):
        """Successful fetch after ERROR state emits watch_recovered."""
        watch = Watch(
            name="Recovering",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.ERROR,
        )
        db_session.add(watch)
        await db_session.commit()
        await db_session.refresh(watch)

        content = b"<html><body>hello</body></html>"
        mock_fetch_result = MagicMock()
        mock_fetch_result.is_success = True
        mock_fetch_result.status_code = 200
        mock_fetch_result.content = content
        mock_fetch_result.fetcher_used = "http"
        mock_fetch_result.duration_ms = 100
        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=mock_fetch_result)

        dispatched_events = []

        async def fake_dispatch(session, event):
            dispatched_events.append(event)

        monkeypatch.setattr("src.workers.tasks.dispatch_event_notifications", fake_dispatch)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_storage = MagicMock()
        mock_storage.snapshot_path = MagicMock(return_value=str(tmp_path / "snap.html"))
        mock_storage.save = MagicMock()
        mock_storage.exists = MagicMock(return_value=False)
        monkeypatch.setattr(tasks_mod, "LocalStorage", lambda **kw: mock_storage)

        reg = ServiceRegistry(fetcher=mock_fetcher)
        await check_watch(str(watch.id), registry=reg)

        await db_session.refresh(watch)
        assert watch.health_status == WatchHealthStatus.OK
        assert any(e.event_type == WatchEventType.WATCH_RECOVERED for e in dispatched_events)

    @pytest.mark.integration
    async def test_change_detected_metadata_includes_domain_and_interval(
        self, db_session, tmp_path, monkeypatch
    ):
        """check_watch enriches change_detected metadata with effective_domain + check_interval."""
        import src.workers.tasks as tasks_mod

        watch = Watch(
            name="Enrichment Test",
            url="https://example.com/enrich",
            content_type=ContentType.HTML,
            effective_domain="example.com",
            schedule_config={"interval": "1h"},
        )
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=50,
            storage=storage,
            session=db_session,
        )
        await db_session.commit()

        mock_response = httpx.Response(
            200,
            content=b"<html><body><p>V2 changed</p></body></html>",
            request=httpx.Request("GET", "https://example.com/enrich"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))
        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(fetcher=HttpFetcher(client=mock_client))
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
        monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        captured_events = []

        async def fake_dispatch(session, event):
            captured_events.append(event)

        monkeypatch.setattr(tasks_mod, "dispatch_event_notifications", fake_dispatch)

        await check_watch(str(watch.id))

        change_events = [e for e in captured_events if e.event_type.value == "change_detected"]
        assert len(change_events) == 1
        assert change_events[0].metadata["effective_domain"] == "example.com"
        assert change_events[0].metadata["check_interval"] == "1h"
