"""Integration tests for dashboard context queries."""

import pytest

from src.core.models.change import Change
from src.core.models.snapshot import Snapshot
from src.core.models.watch import Watch
from src.dashboard.context import (
    get_dashboard_stats,
    get_queue_health,
    get_rate_limiter_state,
    get_recent_changes,
    get_watch_changes,
    get_watch_detail,
    get_watch_list,
)

pytestmark = pytest.mark.integration


class TestGetDashboardStats:
    async def test_empty_database(self, db_session):
        stats = await get_dashboard_stats(db_session)
        assert stats["total_watches"] == 0
        assert stats["active_watches"] == 0
        assert stats["changes_today"] == 0
        assert stats["checks_today"] == 0

    async def test_counts_watches(self, db_session):
        db_session.add(Watch(name="W1", url="https://a.com", content_type="html"))
        db_session.add(
            Watch(
                name="W2",
                url="https://b.com",
                content_type="html",
                is_active=False,
            )
        )
        await db_session.flush()
        stats = await get_dashboard_stats(db_session)
        assert stats["total_watches"] == 2
        assert stats["active_watches"] == 1


class TestGetRecentChanges:
    async def test_empty(self, db_session):
        changes = await get_recent_changes(db_session)
        assert changes == []

    async def test_returns_changes_with_watch_name(self, db_session):
        watch = Watch(name="Test Watch", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        snap_kwargs = dict(
            watch_id=watch.id,
            content_hash="a" * 64,
            simhash=0,
            storage_path="/tmp/s",
            text_path="/tmp/t",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        prev_snap = Snapshot(**snap_kwargs)
        curr_snap = Snapshot(**snap_kwargs)
        db_session.add_all([prev_snap, curr_snap])
        await db_session.flush()

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
            change_metadata={"added": ["Page 1"]},
        )
        db_session.add(change)
        await db_session.flush()
        changes = await get_recent_changes(db_session, limit=10)
        assert len(changes) == 1
        assert changes[0]["watch_name"] == "Test Watch"
        assert changes[0]["id"] is not None


class TestGetQueueHealth:
    async def test_returns_queue_stats(self, db_session):
        queue = await get_queue_health(db_session)
        assert "todo" in queue
        assert "doing" in queue
        assert "failed" in queue
        assert "succeeded_today" in queue


class TestGetRateLimiterState:
    def test_returns_empty_without_limiter(self):
        assert get_rate_limiter_state() == []

    def test_returns_domains_from_limiter(self):
        from src.core.rate_limiter import DomainRateLimiter

        limiter = DomainRateLimiter()
        # Access a domain to create an entry
        limiter.extract_domain("https://example.com")
        _ = limiter._domains["example.com"]
        domains = get_rate_limiter_state(limiter)
        assert isinstance(domains, list)
        assert len(domains) == 1
        assert domains[0]["name"] == "example.com"
        assert domains[0]["in_backoff"] is False


class TestGetWatchList:
    async def test_empty_returns_empty_list(self, db_session):
        result = await get_watch_list(db_session)
        assert result == []

    async def test_returns_watches(self, db_session):
        watch = Watch(name="W1", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        result = await get_watch_list(db_session)
        assert len(result) == 1
        assert result[0].name == "W1"
        assert hasattr(result[0], "last_checked_at")

    async def test_filter_active(self, db_session):
        db_session.add(Watch(name="Active", url="https://a.com", content_type="html"))
        db_session.add(
            Watch(
                name="Inactive",
                url="https://b.com",
                content_type="html",
                is_active=False,
            )
        )
        await db_session.flush()

        active_only = await get_watch_list(db_session, is_active=True)
        assert len(active_only) == 1
        assert active_only[0].name == "Active"


class TestGetWatchDetail:
    async def test_returns_watch(self, db_session):
        watch = Watch(name="Detail", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        result = await get_watch_detail(db_session, str(watch.id))
        assert result is not None
        assert result.name == "Detail"

    async def test_not_found(self, db_session):
        result = await get_watch_detail(db_session, "01JNZZZZZZZZZZZZZZZZZZZZZZ")
        assert result is None

    async def test_invalid_ulid(self, db_session):
        result = await get_watch_detail(db_session, "not-a-ulid")
        assert result is None


class TestGetWatchChanges:
    async def test_empty(self, db_session):
        watch = Watch(name="No Changes", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        result = await get_watch_changes(db_session, str(watch.id))
        assert result == []
