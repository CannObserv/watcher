"""Tests for check_watch pipeline and task wrappers."""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from archiver_client.errors import NotFound
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import src.workers.tasks as tasks_mod
from src.core.fetchers.http import HttpFetcher
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watch import ContentType, Watch, WatchHealthStatus
from src.core.notifications.events import WatchEventType
from src.core.rate_limiter import DomainRateLimiter
from src.core.registry import ServiceRegistry
from src.workers.pipeline import _maybe_decay_backoff
from src.workers.tasks import (
    _persist_backoff,
    _run_check_pipeline,
    _watch_base_metadata,
    check_watch,
    schedule_tick,
)
from tests.conftest import make_watch
from tests.workers.conftest import make_resolved

pytestmark = pytest.mark.integration


class TestWatchBaseMetadata:
    """Unit tests for _watch_base_metadata helper (no DB required)."""

    def _make_watch(self, **kwargs):
        return Watch(name="Test", content_type=ContentType.HTML, **kwargs)

    def test_includes_last_changed_at_when_set(self):
        watch = self._make_watch(last_changed_at=datetime(2026, 4, 9, 14, 22, 37, tzinfo=UTC))
        meta = _watch_base_metadata(watch)
        assert meta["last_changed_at"] == "2026-04-09T14:22:37Z"

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

    def test_includes_tags_when_set(self):
        watch = self._make_watch(tags=["cannabis", "license"])
        meta = _watch_base_metadata(watch)
        assert meta["tags"] == ["cannabis", "license"]

    def test_omits_tags_when_none(self):
        watch = self._make_watch(tags=None)
        meta = _watch_base_metadata(watch)
        assert "tags" not in meta

    def test_omits_tags_when_empty_list(self):
        watch = self._make_watch(tags=[])
        meta = _watch_base_metadata(watch)
        assert "tags" not in meta

    def test_includes_description_when_set(self):
        watch = self._make_watch(description="Monitor for license renewals")
        meta = _watch_base_metadata(watch)
        assert meta["description"] == "Monitor for license renewals"

    def test_omits_description_when_none(self):
        watch = self._make_watch(description=None)
        meta = _watch_base_metadata(watch)
        assert "description" not in meta


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


def _mock_info_client(url: str = "https://example.com"):
    """Build a MagicMock ArchiverClient for resolve_root_sources_with_children.

    Mocks get_info_source (returns root with no parent), list_info_sources
    (returns empty children page), and post_source_revision (returns a fake
    SourceRevisionOut). For tests that intercept the fetcher with httpx MockTransport.
    """
    source_spec = MagicMock()
    source_spec.to_dict = MagicMock(
        return_value={
            "schema_version": 1,
            "target": {"url": url},
            "extraction": {"algorithm": "full_page"},
            "fingerprint": {"algorithm": "simhash"},
        }
    )
    fake_source = MagicMock()
    fake_source.info_source_id = "01TESTSOURCE000000000000XX"
    fake_source.parent_info_source_id = None
    fake_source.source_spec = source_spec

    fake_page = MagicMock()
    fake_page.items = []

    fake_client = MagicMock()
    fake_client.get_info_source = AsyncMock(return_value=fake_source)
    fake_client.list_info_sources = AsyncMock(return_value=fake_page)
    fake_client.post_source_revision = AsyncMock(
        return_value=MagicMock(source_revision_id="01TESTREVISION0000000000RV")
    )
    return fake_client


class TestCheckPipeline:
    """Integration tests for _run_check_pipeline (Phase 5 POST-driven pipeline)."""

    async def test_first_check_creates_snapshot(self, db_session, tmp_path, monkeypatch):
        """First check reports is_changed=True and returns a source_revision_id."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(
            db_session, name="Test", url="https://example.com", content_type=ContentType.HTML
        )
        resolved = make_resolved(info_source_id=str(watch.info_source_id))
        info_client = MagicMock()
        info_client.post_source_revision = AsyncMock(
            return_value=MagicMock(source_revision_id="01HZZ000000000000000000REV")
        )

        from unittest.mock import patch as _patch

        with _patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Hello world</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )
        assert result["is_changed"] is True
        assert result["source_revision_id"] is not None

    async def test_identical_content_no_change(self, db_session, tmp_path, monkeypatch):
        """Second check with identical content reports is_changed=False (fast-path)."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(
            db_session, name="Stable", url="https://example.com", content_type=ContentType.HTML
        )
        resolved = make_resolved(info_source_id=str(watch.info_source_id))
        info_client = MagicMock()
        info_client.post_source_revision = AsyncMock(
            return_value=MagicMock(source_revision_id="01HZZ000000000000000000REV")
        )
        content = b"<html><body><p>Same content</p></body></html>"

        from unittest.mock import patch as _patch

        with _patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            await _run_check_pipeline(
                watch=watch,
                raw_content=content,
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=content,
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )
        assert result["is_changed"] is False
        assert result.get("skipped_reason") == "fast_path"

    async def test_different_content_detects_change(self, db_session, tmp_path, monkeypatch):
        """Different content on second check reports is_changed=True."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(
            db_session, name="Changing", url="https://example.com", content_type=ContentType.HTML
        )
        resolved = make_resolved(info_source_id=str(watch.info_source_id))
        info_client = MagicMock()
        info_client.post_source_revision = AsyncMock(
            side_effect=[
                MagicMock(source_revision_id="01HZZ00000000000000000REV1"),
                MagicMock(source_revision_id="01HZZ00000000000000000REV2"),
            ]
        )

        from unittest.mock import patch as _patch

        with _patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>V1</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>V2</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )
        assert result["is_changed"] is True
        assert result["source_revision_id"] is not None


class TestCheckWatchTask:
    """Tests for the check_watch procrastinate task wrapper.

    Uses monkeypatch to inject test DB session via get_session_factory.
    """

    async def test_429_reports_rate_limit(self, db_session, tmp_path, monkeypatch):
        """A 429 response should report rate limiting and raise ConnectionError."""
        import src.workers.tasks as tasks_mod

        watch = await make_watch(
            db_session,
            name="Rate Limited",
            url="https://example.com/limited",
            content_type=ContentType.HTML,
        )

        mock_response = httpx.Response(
            429,
            content=b"Too Many Requests",
            request=httpx.Request("GET", "https://example.com/limited"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))

        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(
            fetcher=HttpFetcher(client=mock_client),
            archiver_client=_mock_info_client(),
        )
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        with pytest.raises(ConnectionError, match="Rate limited"):
            await check_watch(str(watch.id))

    async def test_inactive_watch_skipped(self, db_session, tmp_path, monkeypatch):
        """Inactive watches should be skipped without fetching."""
        import src.workers.tasks as tasks_mod

        watch = await make_watch(
            db_session,
            name="Inactive",
            url="https://example.com/inactive",
            content_type=ContentType.HTML,
            is_active=False,
        )

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await check_watch(str(watch.id))
        assert result.get("skipped") is True

    async def test_fetch_failure_logs_audit(self, db_session, tmp_path, monkeypatch):
        """Non-success HTTP status should log audit and return error."""
        import src.workers.tasks as tasks_mod

        watch = await make_watch(
            db_session,
            name="Server Error",
            url="https://example.com/error",
            content_type=ContentType.HTML,
        )

        mock_response = httpx.Response(
            500,
            content=b"Internal Server Error",
            request=httpx.Request("GET", "https://example.com/error"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))

        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(
            fetcher=HttpFetcher(client=mock_client),
            archiver_client=_mock_info_client(),
        )
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
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
        """Pipeline DB writes are committed by check_watch after _run_check_pipeline.

        Phase 5: the change_detected dispatch happens inside _run_check_pipeline (at
        pipeline.py, not tasks.py). check_watch commits the session after the pipeline
        returns. Notification failures therefore cannot roll back pipeline state: the
        session is flushed and committed by check_watch regardless.

        This test verifies that check_watch calls session.commit() at least once after
        a successful pipeline run with a detected change.
        """
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(
            db_session,
            name="Savepoint Test",
            url="https://example.com/savepoint",
            content_type=ContentType.HTML,
        )
        await db_session.commit()

        # First check to establish a baseline fingerprint
        baseline_info_client = MagicMock()
        baseline_info_client.post_source_revision = AsyncMock(
            return_value=MagicMock(source_revision_id="01HZZ000000000000000000BAS")
        )
        from unittest.mock import patch as _patch

        with _patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Original</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=50,
                storage=None,
                session=db_session,
                resolved=make_resolved(info_source_id=str(watch.info_source_id)),
                info_client=baseline_info_client,
            )
        await db_session.commit()

        # Second check with changed content — triggers change detection + pipeline dispatch.
        mock_response = httpx.Response(
            200,
            content=b"<html><body><p>Changed content</p></body></html>",
            request=httpx.Request("GET", "https://example.com/savepoint"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))

        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(
            fetcher=HttpFetcher(client=mock_client),
            archiver_client=_mock_info_client(),
        )
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        commit_calls: list[str] = []
        original_commit = db_session.commit

        async def tracking_commit():
            commit_calls.append("commit")
            await original_commit()

        monkeypatch.setattr(db_session, "commit", tracking_commit)

        # Patch both dispatch sites (pipeline.py and tasks.py use different imports).
        pipeline_dispatch_calls: list[str] = []

        async def mock_pipeline_dispatch(session, event):
            pipeline_dispatch_calls.append(event.event_type.value)

        with _patch(
            "src.workers.pipeline.dispatch_event_notifications",
            side_effect=mock_pipeline_dispatch,
        ):
            await check_watch(str(watch.id))

        # Pipeline dispatched at least one change event.
        assert any("change" in e for e in pipeline_dispatch_calls), (
            f"Expected a change_detected event; got: {pipeline_dispatch_calls}"
        )
        # check_watch committed the session after the pipeline (health + timestamp commit).
        assert len(commit_calls) >= 1, "check_watch must commit after pipeline completes"


class TestScheduleTickWithProfiles:
    """Integration tests for schedule_tick temporal profile awareness."""

    async def test_profile_accelerates_check_interval(self, db_session, monkeypatch):
        """A watch with a temporal profile should be deferred sooner than its base interval."""
        import src.workers.tasks as tasks_mod

        # Watch with 1-day base interval, last checked 2 hours ago
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        watch = await make_watch(
            db_session,
            name="Profiled",
            url="https://example.com/agenda",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=2),
        )

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
        watch = await make_watch(
            db_session,
            name="Expired Event",
            url="https://example.com/past-event",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=25),
        )

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
        watch = await make_watch(
            db_session,
            name="Archive Event",
            url="https://example.com/archive-event",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=25),
        )

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
        watch = await make_watch(
            db_session,
            name="Deactivate Event",
            url="https://example.com/deact-event",
            content_type=ContentType.HTML,
            schedule_config={"interval": "1d"},
            last_checked_at=now - timedelta(hours=25),
        )

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
        await make_watch(
            db_session,
            name="On Paused Domain",
            url="https://paused.com/p",
            content_type=ContentType.HTML,
            effective_domain="paused.com",
            is_active=True,
        )
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
        watch = await make_watch(
            db_session,
            name="On Active Domain",
            url="https://active-ctrl.com/p",
            content_type=ContentType.HTML,
            effective_domain="active-ctrl.com",
            is_active=True,
        )
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
        watch = await make_watch(
            db_session,
            name="Domain Inactive Watch",
            url="https://skipped.com/p",
            content_type=ContentType.HTML,
            effective_domain="skipped.com",
            is_active=True,
        )
        await db_session.commit()

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
        watch = await make_watch(
            db_session,
            name="Health Test",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.OK,
        )
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

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=_mock_info_client())
        await check_watch(str(watch.id), registry=reg)

        await db_session.refresh(watch)
        assert watch.health_status == WatchHealthStatus.ERROR
        assert any(e.event_type == WatchEventType.WATCH_ERROR for e in dispatched_events)

    async def test_repeated_failure_does_not_emit_watch_error_again(self, db_session, monkeypatch):
        """Repeated failures after first do NOT re-emit watch_error."""
        watch = await make_watch(
            db_session,
            name="Already Error",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.ERROR,
        )
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

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=_mock_info_client())
        await check_watch(str(watch.id), registry=reg)

        assert not any(e.event_type == WatchEventType.WATCH_ERROR for e in dispatched_events)

    async def test_recovery_emits_watch_recovered(self, db_session, monkeypatch, tmp_path):
        """Successful fetch after ERROR state emits watch_recovered."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(
            db_session,
            name="Recovering",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.ERROR,
        )
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

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=_mock_info_client())
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

        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(
            db_session,
            name="Enrichment Test",
            url="https://example.com/enrich",
            content_type=ContentType.HTML,
            effective_domain="example.com",
            schedule_config={"interval": "1h"},
        )

        baseline_info_client = MagicMock()
        baseline_info_client.post_source_revision = AsyncMock(
            return_value=MagicMock(source_revision_id="01HZZ000000000000000000BAS")
        )
        from unittest.mock import patch as _patch

        with _patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>V1</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=50,
                storage=None,
                session=db_session,
                resolved=make_resolved(info_source_id=str(watch.info_source_id)),
                info_client=baseline_info_client,
            )
        await db_session.commit()

        mock_response = httpx.Response(
            200,
            content=b"<html><body><p>V2 changed</p></body></html>",
            request=httpx.Request("GET", "https://example.com/enrich"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))
        fast_limiter = DomainRateLimiter(min_interval=0.0)
        mock_registry = ServiceRegistry(
            fetcher=HttpFetcher(client=mock_client),
            archiver_client=_mock_info_client(),
        )
        monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
        monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        captured_events = []

        async def fake_dispatch(session, event):
            captured_events.append(event)

        # Phase 5: change_detected is dispatched from pipeline.py, not tasks.py.
        from unittest.mock import patch as _patch

        with _patch("src.workers.pipeline.dispatch_event_notifications", side_effect=fake_dispatch):
            await check_watch(str(watch.id))

        change_events = [e for e in captured_events if e.event_type.value == "change_detected"]
        assert len(change_events) == 1
        assert change_events[0].metadata["effective_domain"] == "example.com"
        assert change_events[0].metadata["check_interval"] == "1h"


def _make_fake_source(
    *,
    info_source_id: str = "01TESTSOURCE000000000000XX",
    url: str = "https://from-spec.example.com",
    timeout_seconds: int | None = None,
    render: bool | None = None,
    extraction_algorithm: str = "full_page",
    selector: str | None = None,
):
    """Build a MagicMock matching archiver_client.InfoSourceOut shape for the root source."""
    fetch_block: dict = {}
    if timeout_seconds is not None:
        fetch_block["timeout_seconds"] = timeout_seconds
    if render is not None:
        fetch_block["render"] = render
    target: dict = {"url": url}
    if fetch_block:
        target["fetch"] = fetch_block
    extraction: dict = {"algorithm": extraction_algorithm}
    if selector is not None:
        extraction["selector"] = selector
    source_spec_dict = {
        "schema_version": 1,
        "target": target,
        "extraction": extraction,
        "fingerprint": {"algorithm": "simhash"},
    }
    fake_source_spec = MagicMock()
    fake_source_spec.to_dict = MagicMock(return_value=source_spec_dict)
    fake_source = MagicMock()
    fake_source.info_source_id = info_source_id
    fake_source.parent_info_source_id = None
    fake_source.source_spec = fake_source_spec
    return fake_source


def _make_fake_info_client(
    source: MagicMock | None = None, *, url: str = "https://from-spec.example.com"
):
    """Build a MagicMock ArchiverClient wired for resolve_root_sources_with_children."""
    if source is None:
        source = _make_fake_source(url=url)
    fake_page = MagicMock()
    fake_page.items = []
    fake_client = MagicMock()
    fake_client.get_info_source = AsyncMock(return_value=source)
    fake_client.list_info_sources = AsyncMock(return_value=fake_page)
    return fake_client


def _fake_fetch_result(
    *,
    content: bytes = b"<html><body><p>hi</p></body></html>",
    status_code: int = 200,
    fetcher_used: str = "http",
    duration_ms: int = 10,
):
    """Build a MagicMock matching FetchResult shape."""
    result = MagicMock()
    result.content = content
    result.status_code = status_code
    result.is_success = 200 <= status_code < 400
    result.fetcher_used = fetcher_used
    result.duration_ms = duration_ms
    result.headers = {}
    return result


class TestCheckWatchResolvesUrlViaSdk:
    """check_watch must resolve URL/fetch defaults from the InfoSource, not the Watch row."""

    async def test_check_watch_resolves_url_via_sdk(self, db_session, tmp_path, monkeypatch):
        """check_watch fetches the URL from the root InfoSource, not from the Watch row."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(db_session, name="SdkUrl", url="https://from-spec.example.com")

        fetch_mock = AsyncMock(return_value=_fake_fetch_result())
        mock_fetcher = MagicMock()
        mock_fetcher.fetch = fetch_mock

        fake_source = _make_fake_source(
            url="https://from-spec.example.com",
            timeout_seconds=45,
        )
        fake_client = _make_fake_info_client(source=fake_source)

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=fake_client)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        await check_watch(str(watch.id), registry=reg)

        fetch_mock.assert_awaited_once()
        args, kwargs = fetch_mock.call_args
        # First positional arg or a `url=` kwarg should be the source URL.
        passed_url = args[0] if args else kwargs.get("url")
        assert passed_url == "https://from-spec.example.com"
        config = kwargs.get("config") or {}
        assert config.get("timeout") == 45

    async def test_check_watch_skips_when_info_item_missing(
        self, db_session, tmp_path, monkeypatch
    ):
        """If the SDK 404s on info_source lookup, the watch is skipped (not retried)."""
        watch = await make_watch(db_session, name="Missing")

        mock_fetcher = MagicMock()
        mock_fetcher.fetch = AsyncMock()

        fake_client = MagicMock()
        fake_client.get_info_source = AsyncMock(
            side_effect=NotFound("info_source not found", status_code=404, body="")
        )
        fake_client.list_info_sources = AsyncMock()

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=fake_client)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        result = await check_watch(str(watch.id), registry=reg)

        assert result == {"skipped": True, "reason": "info_item_missing"}
        mock_fetcher.fetch.assert_not_called()

    async def test_check_watch_propagates_connection_error(self, db_session, tmp_path, monkeypatch):
        """ConnectionError from the SDK propagates so Procrastinate retries."""
        watch = await make_watch(db_session, name="ConnErr")

        mock_fetcher = MagicMock()
        mock_fetcher.fetch = AsyncMock()

        fake_client = MagicMock()
        fake_client.get_info_source = AsyncMock(
            side_effect=httpx.ConnectError("Information service down")
        )
        fake_client.list_info_sources = AsyncMock()

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=fake_client)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        with pytest.raises(httpx.ConnectError):
            await check_watch(str(watch.id), registry=reg)

    async def test_check_watch_zero_chunks_proceeds_without_retry(
        self, db_session, tmp_path, monkeypatch
    ):
        """Zero-chunk extraction no longer retries (force_refresh removed in Task 7.1).

        Task 7.2 will implement child-source fallback. For now, the pipeline
        accepts zero chunks and proceeds to diff.
        """
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(db_session, name="ZeroChunks")

        # Content with no matching selector — extraction returns 0 chunks.
        content = b"<html><body><section>real</section></body></html>"
        fetch_mock = AsyncMock(return_value=_fake_fetch_result(content=content))
        mock_fetcher = MagicMock()
        mock_fetcher.fetch = fetch_mock

        fake_source = _make_fake_source(
            url="https://example.com/zeochunks",
            extraction_algorithm="css",
            selector=".does-not-exist",
        )
        fake_client = _make_fake_info_client(source=fake_source)

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=fake_client)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        await check_watch(str(watch.id), registry=reg)

        # Fetcher called only once (no re-fetch, no retry).
        assert fetch_mock.await_count == 1
        # SDK get_info_source called once (no force_refresh retry).
        assert fake_client.get_info_source.await_count == 1


class TestCheckWatchUsesNewResolver:
    """check_watch must call resolve_root_sources_with_children, not resolve_primary."""

    @pytest.mark.asyncio
    async def test_check_watch_uses_resolve_root_sources(self, db_session, tmp_path, monkeypatch):
        """check_watch resolves via resolve_root_sources_with_children, not resolve_primary."""
        from src.core.sources.resolver import ResolvedRootSource

        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(
            db_session,
            name="NewResolver",
            url="https://example.com/newresolver",
            content_type=ContentType.HTML,
        )

        fake_resolved = ResolvedRootSource(
            info_source_id=str(watch.info_source_id),
            url="https://example.com/newresolver",
            source_spec={
                "schema_version": 1,
                "target": {"url": "https://example.com/newresolver"},
                "extraction": {"algorithm": "full_page"},
                "fingerprint": {"algorithm": "simhash"},
            },
            children=[],
        )

        content = b"<html><body><p>resolver test</p></body></html>"
        fetch_mock = AsyncMock(return_value=_fake_fetch_result(content=content))
        mock_fetcher = MagicMock()
        mock_fetcher.fetch = fetch_mock

        fake_client = MagicMock()

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=fake_client)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        with patch(
            "src.workers.tasks.resolve_root_sources_with_children",
            new=AsyncMock(return_value=fake_resolved),
        ) as mock_resolve:
            await check_watch(str(watch.id), registry=reg)

        mock_resolve.assert_awaited_once()
