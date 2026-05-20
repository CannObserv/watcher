"""Tests for `check_watched_item` and `schedule_tick` (Task 8, #160).

The per-Watch `check_watch` is gone; `check_watched_item` is the new periodic
task.  `schedule_tick` now enqueues per-WatchedItem jobs, aggregating
`last_checked_at` across the WatchedItem's child Watches.
"""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import src.workers.tasks as tasks_mod
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watch import ContentType, WatchHealthStatus
from src.core.rate_limiter import DomainRateLimiter
from src.core.registry import ServiceRegistry
from src.core.watches.resolution import resolved_schedule_config
from src.workers.pipeline import WatchedItemResult, _maybe_decay_backoff, _persist_backoff
from src.workers.tasks import check_watched_item, schedule_tick
from tests.conftest import make_watch

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_factory(db_session: AsyncSession):
    """Return a session-factory stand-in yielding the test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


def _fake_fetch_result(
    *,
    content: bytes = b"<html><body><p>hi</p></body></html>",
    status_code: int = 200,
    fetcher_used: str = "http",
    duration_ms: int = 10,
):
    """MagicMock matching FetchResult shape."""
    result = MagicMock()
    result.content = content
    result.status_code = status_code
    result.is_success = 200 <= status_code < 400
    result.fetcher_used = fetcher_used
    result.duration_ms = duration_ms
    result.headers = {}
    return result


def _make_pipeline_stub(*, posted: int = 0, changed_ids: list[str] | None = None) -> AsyncMock:
    """Return an AsyncMock that mimics `process_watched_item`'s return shape."""

    async def _proc(session, info_client, watched_item, *, raw_content, bindings=None):
        return WatchedItemResult(
            bindings_processed=1,
            revisions_posted=posted,
            changed_info_source_ids=changed_ids or [],
        )

    return AsyncMock(side_effect=_proc)


# ---------------------------------------------------------------------------
# _persist_backoff / _maybe_decay_backoff regression tests (preserved).
# ---------------------------------------------------------------------------


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

        await _persist_backoff("unknown.com", 4.0, mock_session)


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


# ---------------------------------------------------------------------------
# check_watched_item
# ---------------------------------------------------------------------------


class TestCheckWatchedItem:
    """The periodic per-WatchedItem task wires fetcher → pipeline → timestamps."""

    async def test_updates_last_checked_at_on_every_child_watch(self, db_session, monkeypatch):
        """Every active child Watch should have last_checked_at set, even if its
        target binding did not change in this cycle."""
        wi_watch = await make_watch(db_session, name="Primary")
        watched_item = wi_watch.watched_item

        # Sibling Watch under the same WatchedItem (target unchanged here).
        sibling = await make_watch(
            db_session,
            name="Sibling",
            info_item_id=watched_item.info_item_id,
            watched_item=watched_item,
        )
        await db_session.commit()

        before = datetime.now(UTC)

        # Stub fetcher + info_client; pipeline is mocked.
        fetch_mock = AsyncMock(return_value=_fake_fetch_result())
        mock_fetcher = MagicMock()
        mock_fetcher.fetch = fetch_mock

        fake_client = MagicMock()
        # Primary URL resolution
        primary_binding = MagicMock()
        primary_binding.info_source_id = "01PRIMARYSOURCE000000000XX"
        primary_binding.url = "https://example.com/page"
        fake_client._noop = None  # placeholder; pipeline-call is stubbed below.

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=fake_client)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        monkeypatch.setattr(
            tasks_mod, "get_rate_limiter", lambda: DomainRateLimiter(min_interval=0.0)
        )
        # Resolve primary URL via a thin patch — caller-side fetch needs *some* URL.
        monkeypatch.setattr(
            tasks_mod,
            "fetch_info_item_bindings",
            AsyncMock(
                return_value=MagicMock(
                    primary=primary_binding,
                    primary_url="https://example.com/page",
                    cross_checks=[],
                    sub_aspects=[],
                )
            ),
        )
        monkeypatch.setattr(tasks_mod, "process_watched_item", _make_pipeline_stub())

        await check_watched_item(str(watched_item.id), registry=reg)

        await db_session.refresh(wi_watch)
        await db_session.refresh(sibling)
        assert wi_watch.last_checked_at is not None
        assert wi_watch.last_checked_at >= before
        assert sibling.last_checked_at is not None
        assert sibling.last_checked_at >= before

        await db_session.refresh(watched_item)
        assert watched_item.last_checked_at is not None
        assert watched_item.last_checked_at >= before

    async def test_noop_when_no_active_children(self, db_session, monkeypatch):
        """A WatchedItem with no active child Watches skips fetcher + pipeline."""
        watch = await make_watch(db_session, name="Inactive", is_active=False)
        watched_item = watch.watched_item
        await db_session.commit()

        fetch_mock = AsyncMock(return_value=_fake_fetch_result())
        mock_fetcher = MagicMock()
        mock_fetcher.fetch = fetch_mock

        proc_mock = _make_pipeline_stub()
        monkeypatch.setattr(tasks_mod, "process_watched_item", proc_mock)
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=MagicMock())
        result = await check_watched_item(str(watched_item.id), registry=reg)

        assert result.get("skipped") is True
        fetch_mock.assert_not_called()
        proc_mock.assert_not_called()

    async def test_fetch_failure_logs_audit_and_sets_health(self, db_session, monkeypatch):
        """A non-success HTTP response audits CHECK_FETCH_FAILED and marks Watches ERROR."""
        watch = await make_watch(db_session, name="Fails")
        watched_item = watch.watched_item
        await db_session.commit()

        mock_fetcher = MagicMock()
        mock_fetcher.fetch = AsyncMock(
            return_value=_fake_fetch_result(content=b"err", status_code=500)
        )

        primary_binding = MagicMock()
        primary_binding.info_source_id = "01PRIMARYSOURCE000000000XX"
        primary_binding.url = "https://example.com/page"
        monkeypatch.setattr(
            tasks_mod,
            "fetch_info_item_bindings",
            AsyncMock(
                return_value=MagicMock(
                    primary=primary_binding,
                    primary_url="https://example.com/page",
                    cross_checks=[],
                    sub_aspects=[],
                )
            ),
        )
        monkeypatch.setattr(tasks_mod, "process_watched_item", _make_pipeline_stub())
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        monkeypatch.setattr(
            tasks_mod, "get_rate_limiter", lambda: DomainRateLimiter(min_interval=0.0)
        )

        reg = ServiceRegistry(fetcher=mock_fetcher, archiver_client=MagicMock())
        result = await check_watched_item(str(watched_item.id), registry=reg)
        assert "error" in result

        audit_rows = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.CHECK_FETCH_FAILED)
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1

        await db_session.refresh(watch)
        assert watch.health_status == WatchHealthStatus.ERROR


# ---------------------------------------------------------------------------
# schedule_tick (per-WatchedItem aggregation).
# ---------------------------------------------------------------------------


class TestScheduleTickAggregation:
    """schedule_tick enqueues per-WatchedItem based on MIN(child last_checked_at)."""

    async def test_enqueues_when_min_child_overdue(self, db_session, monkeypatch):
        """A WatchedItem whose earliest child check is older than the interval is due."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        # Default schedule on the WatchedItem = 1h.
        watch_a = await make_watch(db_session, name="A")
        wi = watch_a.watched_item
        wi.default_schedule_config = {"interval": "1h"}
        watch_a.last_checked_at = now - timedelta(hours=2)
        watch_b = await make_watch(
            db_session,
            name="B",
            info_item_id=wi.info_item_id,
            watched_item=wi,
        )
        watch_b.last_checked_at = now - timedelta(minutes=5)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        defer_calls: list = []
        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock(
            side_effect=lambda **kw: defer_calls.append(kw)
        )
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        assert len(defer_calls) == 1
        assert defer_calls[0]["watched_item_id"] == str(wi.id)

    async def test_does_not_enqueue_when_all_children_fresh(self, db_session, monkeypatch):
        """If every child Watch was checked recently, the WatchedItem is not due."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        w = await make_watch(db_session, name="Fresh")
        wi = w.watched_item
        wi.default_schedule_config = {"interval": "1h"}
        w.last_checked_at = now - timedelta(minutes=5)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock()
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        mock_configure.return_value.defer_async.assert_not_called()

    async def test_null_last_checked_at_is_due_immediately(self, db_session, monkeypatch):
        """A Watch with NULL last_checked_at means the WatchedItem is overdue."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        w = await make_watch(db_session, name="Never")
        wi = w.watched_item
        wi.default_schedule_config = {"interval": "1h"}
        # explicitly leave last_checked_at as None.
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        defer_calls: list = []
        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock(
            side_effect=lambda **kw: defer_calls.append(kw)
        )
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        assert len(defer_calls) == 1
        assert defer_calls[0]["watched_item_id"] == str(wi.id)

    async def test_skips_inactive_or_archived_watched_items(self, db_session, monkeypatch):
        """is_active=False and archived_at IS NOT NULL exclude a WatchedItem."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        w_inactive = await make_watch(
            db_session, name="Inactive WI", primary_url="https://inactive.example.com"
        )
        w_inactive.watched_item.is_active = False
        w_archived = await make_watch(
            db_session, name="Archived WI", primary_url="https://archived.example.com"
        )
        w_archived.watched_item.archived_at = now - timedelta(days=1)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock()
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        mock_configure.return_value.defer_async.assert_not_called()


class TestScheduleTickInactiveDomain:
    """schedule_tick excludes WatchedItems whose primary domain is inactive."""

    async def test_skips_when_all_children_on_inactive_domain(self, db_session, monkeypatch):
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        domain = Domain(name="paused.com", is_active=False)
        db_session.add(domain)
        await make_watch(
            db_session,
            name="On Paused Domain",
            effective_domain="paused.com",
            is_active=True,
        )
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock()
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        mock_configure.return_value.defer_async.assert_not_called()


# ---------------------------------------------------------------------------
# Post-actions: reduce_frequency now mutates the parent WatchedItem.
# ---------------------------------------------------------------------------


class TestPostActions:
    """reduce_frequency must mutate the parent WatchedItem so sibling Watches feel it."""

    async def test_reduce_frequency_mutates_watched_item_default(self, db_session, monkeypatch):
        """post_action=reduce_frequency on one Watch slows all siblings via WatchedItem."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)

        # WatchedItem starts at 1h interval.
        w_target = await make_watch(db_session, name="Profiled")
        wi = w_target.watched_item
        wi.default_schedule_config = {"interval": "1h"}
        w_target.last_checked_at = now - timedelta(hours=25)

        # Sibling sharing the same WatchedItem.
        sibling = await make_watch(
            db_session,
            name="Sibling",
            info_item_id=wi.info_item_id,
            watched_item=wi,
        )
        sibling.last_checked_at = now - timedelta(hours=25)

        # Expired event profile attached to w_target only — reduce_frequency.
        profile = TemporalProfile(
            watch_id=w_target.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 5, 1),  # in the past
            rules=[{"days_before": 7, "interval": "1m"}],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        db_session.add(profile)
        await db_session.commit()

        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )

        mock_configure = MagicMock()
        mock_configure.return_value.defer_async = AsyncMock()
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        # WatchedItem's default_schedule_config reduced to 1d.
        await db_session.refresh(wi)
        assert wi.default_schedule_config.get("interval") == "1d"

        # Both siblings now resolve to the 1d interval (via inheritance).
        await db_session.refresh(w_target)
        await db_session.refresh(sibling)
        assert resolved_schedule_config(w_target).get("interval") == "1d"
        assert resolved_schedule_config(sibling).get("interval") == "1d"

        # Audit log: WATCHED_ITEM_THROTTLED for the parent.
        audit_rows = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_THROTTLED)
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1
        assert audit_rows[0].payload["new_interval"] == "1d"

    async def test_deactivate_post_action_deactivates_watch(self, db_session, monkeypatch):
        """deactivate still flips the triggering Watch is_active off."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        w = await make_watch(db_session, name="Will Deactivate")
        w.watched_item.default_schedule_config = {"interval": "1d"}
        w.last_checked_at = now - timedelta(hours=25)

        profile = TemporalProfile(
            watch_id=w.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 5, 1),
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
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        await db_session.refresh(w)
        assert w.is_active is False
        await db_session.refresh(profile)
        assert profile.is_active is False

    async def test_archive_post_action_sets_is_archived(self, db_session, monkeypatch):
        """archive flips is_active=False AND is_archived=True on the Watch."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        w = await make_watch(db_session, name="Will Archive")
        w.watched_item.default_schedule_config = {"interval": "1d"}
        w.last_checked_at = now - timedelta(hours=25)

        profile = TemporalProfile(
            watch_id=w.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 5, 1),
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
        monkeypatch.setattr(check_watched_item, "configure", mock_configure)

        with patch("src.workers.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await schedule_tick(int(now.timestamp()))

        await db_session.refresh(w)
        assert w.is_active is False
        assert w.is_archived is True


# ---------------------------------------------------------------------------
# Smoke test — type sanity, no DB.
# ---------------------------------------------------------------------------


class TestContentTypeSanity:
    def test_content_type_enum_still_html(self):
        assert ContentType.HTML == "html"
