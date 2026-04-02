"""Integration tests for dashboard context queries."""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.change import Change
from src.core.models.domain import Domain
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch
from src.dashboard.context import (
    compute_watch_health,
    generate_diff,
    get_change_detail,
    get_dashboard_stats,
    get_domains_with_watch_counts,
    get_queue_health,
    get_rate_limiter_state,
    get_recent_changes,
    get_watch_changes,
    get_watch_detail,
    get_watch_health_map,
    get_watch_list,
    get_watch_timeline,
    summarize_change_metadata,
)


@pytest.mark.integration
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


@pytest.mark.integration
class TestGetRecentChanges:
    async def test_empty(self, db_session):
        changes = await get_recent_changes(db_session)
        assert changes == []

    async def test_returns_changes_with_watch_name(
        self, db_session, make_watch, make_snapshot, make_change
    ):
        snap_defaults = dict(
            content_hash="a" * 64,
            simhash=0,
            storage_path="/tmp/s",
            text_path="/tmp/t",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
        )
        watch = await make_watch(name="Test Watch")
        prev_snap = await make_snapshot(watch, **snap_defaults)
        curr_snap = await make_snapshot(watch, **snap_defaults)
        await make_change(watch, curr_snap, prev_snap, change_metadata={"added": ["Page 1"]})

        changes = await get_recent_changes(db_session, limit=10)
        assert len(changes) == 1
        assert changes[0]["watch_name"] == "Test Watch"
        assert changes[0]["id"] is not None


@pytest.mark.integration
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


@pytest.mark.integration
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

    async def test_excludes_archived_by_default(self, db_session):
        db_session.add(Watch(name="Normal", url="https://a.com", content_type="html"))
        db_session.add(
            Watch(
                name="Archived",
                url="https://b.com",
                content_type="html",
                is_active=False,
                is_archived=True,
            )
        )
        await db_session.flush()

        result = await get_watch_list(db_session)
        names = [w.name for w in result]
        assert "Normal" in names
        assert "Archived" not in names

    async def test_include_archived_returns_all(self, db_session):
        db_session.add(Watch(name="Normal", url="https://a.com", content_type="html"))
        db_session.add(
            Watch(
                name="Archived",
                url="https://b.com",
                content_type="html",
                is_active=False,
                is_archived=True,
            )
        )
        await db_session.flush()

        result = await get_watch_list(db_session, include_archived=True)
        names = [w.name for w in result]
        assert "Normal" in names
        assert "Archived" in names


@pytest.mark.integration
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


@pytest.mark.integration
class TestGetChangeDetail:
    async def test_returns_change_with_snapshots(self, db_session):
        watch = Watch(name="W", url="https://example.com", content_type="html")
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

        chunk = SnapshotChunk(
            snapshot_id=curr_snap.id,
            chunk_index=0,
            chunk_type="section",
            chunk_label="Main",
            content_hash="b" * 64,
            simhash=0,
            char_count=100,
            excerpt="Hello world",
        )
        db_session.add(chunk)

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
            change_metadata={"added": ["Section A"], "modified": [], "removed": []},
        )
        db_session.add(change)
        await db_session.flush()

        result = await get_change_detail(db_session, str(change.id))
        assert result is not None
        assert result["change"] is not None
        assert result["watch_name"] == "W"
        assert result["current_snapshot"] is not None
        assert len(result["current_chunks"]) == 1

    async def test_not_found(self, db_session):
        result = await get_change_detail(db_session, "01JNZZZZZZZZZZZZZZZZZZZZZZ")
        assert result is None

    async def test_invalid_id(self, db_session):
        result = await get_change_detail(db_session, "bad")
        assert result is None

    async def test_returns_visual_change_score_when_set(self, db_session):
        watch = Watch(name="W2", url="https://example.com", content_type="html")
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
            visual_change_score=0.75,
        )
        db_session.add(change)
        await db_session.flush()

        result = await get_change_detail(db_session, str(change.id))
        assert result is not None
        assert result["change"].visual_change_score == pytest.approx(0.75)

    async def test_snapshots_expose_screenshot_path(self, db_session):
        watch = Watch(name="W3", url="https://example.com", content_type="html")
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
        prev_snap = Snapshot(**snap_kwargs, screenshot_path="screenshots/w/prev.png")
        curr_snap = Snapshot(**snap_kwargs, screenshot_path="screenshots/w/curr.png")
        db_session.add_all([prev_snap, curr_snap])
        await db_session.flush()

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
        )
        db_session.add(change)
        await db_session.flush()

        result = await get_change_detail(db_session, str(change.id))
        assert result is not None
        assert result["previous_snapshot"].screenshot_path == "screenshots/w/prev.png"
        assert result["current_snapshot"].screenshot_path == "screenshots/w/curr.png"


@pytest.mark.integration
class TestGetWatchChanges:
    async def test_empty(self, db_session):
        watch = Watch(name="No Changes", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        result = await get_watch_changes(db_session, str(watch.id))
        assert result == []


class TestSummarizeChangeMetadata:
    def test_all_counts(self):
        meta = {"added": ["a", "b"], "modified": ["c"], "removed": ["d", "e", "f"]}
        assert summarize_change_metadata(meta) == "2 added, 1 modified, 3 removed"

    def test_only_added(self):
        assert summarize_change_metadata({"added": ["x"]}) == "1 added"

    def test_only_modified(self):
        assert summarize_change_metadata({"modified": ["x", "y"]}) == "2 modified"

    def test_only_removed(self):
        assert summarize_change_metadata({"removed": ["x"]}) == "1 removed"

    def test_empty_metadata(self):
        assert summarize_change_metadata({}) == "change detected"

    def test_zero_counts(self):
        meta = {"added": [], "modified": [], "removed": []}
        assert summarize_change_metadata(meta) == "change detected"

    def test_missing_keys(self):
        assert summarize_change_metadata({"added": ["x"], "removed": []}) == "1 added"


class TestGenerateDiff:
    def test_identical_text(self):
        result = generate_diff("hello\nworld", "hello\nworld")
        assert result["has_changes"] is False

    def test_modified_text(self):
        result = generate_diff("hello\nworld", "hello\nplanet")
        assert result["has_changes"] is True
        assert len(result["lines"]) > 0

    def test_empty_previous(self):
        result = generate_diff("", "new content")
        assert result["has_changes"] is True

    def test_empty_both(self):
        result = generate_diff("", "")
        assert result["has_changes"] is False


@pytest.mark.integration
class TestGetDomainsWithWatchCounts:
    async def test_empty_domains(self, db_session):
        result = await get_domains_with_watch_counts(db_session)
        assert result == []

    async def test_domain_with_watches(self, db_session):
        domain = Domain(name="example.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        watch = Watch(
            name="Test",
            url="https://example.com",
            content_type=ContentType.HTML,
            effective_domain="example.com",
        )
        db_session.add(watch)
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert len(result) == 1
        assert result[0]["name"] == "example.com"
        assert result[0]["watch_count"] == 1
        assert result[0]["in_backoff"] is False

    async def test_domain_with_no_watches(self, db_session):
        domain = Domain(name="orphan.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert len(result) == 1
        assert result[0]["watch_count"] == 0

    async def test_domain_in_backoff(self, db_session):
        domain = Domain(name="slow.com", min_interval=1.0, max_concurrency=2, current_interval=4.0)
        db_session.add(domain)
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["in_backoff"] is True
        assert result[0]["current_interval"] == 4.0


@pytest.mark.integration
class TestGetDomainsFiltered:
    async def test_search_by_name(self, db_session):
        db_session.add(Domain(name="alpha.com"))
        db_session.add(Domain(name="beta.com"))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session, search="alpha")
        assert len(result) == 1
        assert result[0]["name"] == "alpha.com"

    async def test_filter_active_excludes_archived(self, db_session):
        db_session.add(Domain(name="active.com"))
        db_session.add(Domain(name="gone.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session, status="active")
        names = [d["name"] for d in result]
        assert "active.com" in names
        assert "gone.com" not in names

    async def test_filter_archived(self, db_session):
        db_session.add(Domain(name="live.com"))
        db_session.add(Domain(name="gone.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session, status="archived")
        names = [d["name"] for d in result]
        assert "gone.com" in names
        assert "live.com" not in names

    async def test_filter_backoff(self, db_session):
        db_session.add(Domain(name="normal.com"))
        db_session.add(Domain(name="slow.com", current_interval=5.0))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session, status="backoff")
        names = [d["name"] for d in result]
        assert "slow.com" in names
        assert "normal.com" not in names

    async def test_pagination(self, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"dom{i:02d}.com"))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session, page=1, page_size=2)
        assert len(result) == 2
        assert result[0]["name"] == "dom00.com"

    async def test_pagination_page_2(self, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"dom{i:02d}.com"))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session, page=2, page_size=2)
        assert len(result) == 2
        assert result[0]["name"] == "dom02.com"

    async def test_last_checked_from_watches(self, db_session):
        domain = Domain(name="checked.com")
        db_session.add(domain)
        now = datetime.now(UTC)
        watch = Watch(
            name="W",
            url="https://checked.com",
            content_type="html",
            effective_domain="checked.com",
            last_checked_at=now,
        )
        db_session.add(watch)
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["last_checked"] == now

    async def test_last_checked_none_when_no_watches(self, db_session):
        db_session.add(Domain(name="orphan.com"))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["last_checked"] is None

    async def test_result_includes_status(self, db_session):
        db_session.add(Domain(name="s.com"))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["status"] == "active"

    async def test_result_includes_notes(self, db_session):
        db_session.add(Domain(name="n.com", notes="important"))
        await db_session.flush()
        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["notes"] == "important"


class TestComputeWatchHealth:
    """Unit tests for compute_watch_health pure function."""

    def _make_watch(self, interval="1h", last_checked_at=None):
        return Watch(
            name="W",
            url="https://example.com",
            content_type="html",
            schedule_config={"interval": interval} if interval else {},
            last_checked_at=last_checked_at,
        )

    def test_unknown_when_never_checked(self):
        watch = self._make_watch(last_checked_at=None)
        now = datetime.now(UTC)
        assert compute_watch_health(watch, None, now) == "unknown"

    def test_error_when_latest_event_is_fetch_failed(self):
        now = datetime.now(UTC)
        watch = self._make_watch(last_checked_at=now - timedelta(minutes=30))
        assert compute_watch_health(watch, EventType.CHECK_FETCH_FAILED, now) == "error"

    def test_healthy_when_checked_within_interval(self):
        now = datetime.now(UTC)
        # interval=1h, checked 30m ago → well within 2× (2h)
        watch = self._make_watch(interval="1h", last_checked_at=now - timedelta(minutes=30))
        assert compute_watch_health(watch, EventType.CHECK_NO_CHANGE, now) == "healthy"

    def test_healthy_when_snapshot_created(self):
        now = datetime.now(UTC)
        watch = self._make_watch(interval="1h", last_checked_at=now - timedelta(minutes=30))
        assert compute_watch_health(watch, EventType.CHECK_SNAPSHOT_CREATED, now) == "healthy"

    def test_warning_when_stale_beyond_2x_interval(self):
        now = datetime.now(UTC)
        # interval=1h, checked 3h ago → 3h > 2×1h
        watch = self._make_watch(interval="1h", last_checked_at=now - timedelta(hours=3))
        assert compute_watch_health(watch, EventType.CHECK_NO_CHANGE, now) == "warning"

    def test_healthy_at_exactly_2x_boundary(self):
        now = datetime.now(UTC)
        # checked exactly 2h ago with 1h interval → at boundary → healthy (not strictly >)
        watch = self._make_watch(interval="1h", last_checked_at=now - timedelta(hours=2))
        assert compute_watch_health(watch, EventType.CHECK_NO_CHANGE, now) == "healthy"

    def test_warning_just_past_2x_boundary(self):
        now = datetime.now(UTC)
        watch = self._make_watch(interval="1h", last_checked_at=now - timedelta(hours=2, seconds=1))
        assert compute_watch_health(watch, EventType.CHECK_NO_CHANGE, now) == "warning"

    def test_no_interval_uses_default_1d(self):
        now = datetime.now(UTC)
        # No interval configured → default 1d; checked 1h ago → healthy
        watch = self._make_watch(interval=None, last_checked_at=now - timedelta(hours=1))
        assert compute_watch_health(watch, EventType.CHECK_NO_CHANGE, now) == "healthy"

    def test_unknown_when_last_event_is_none_but_checked_at_set(self):
        # last_checked_at is set but no audit event in DB (edge case) → treat as unknown
        now = datetime.now(UTC)
        watch = self._make_watch(interval="1h", last_checked_at=now - timedelta(minutes=10))
        # If no check event found at all, return unknown
        assert compute_watch_health(watch, None, now) == "unknown"


@pytest.mark.integration
class TestGetWatchHealthMap:
    """Integration tests for get_watch_health_map."""

    async def test_empty_returns_empty_dict(self, db_session):
        result = await get_watch_health_map(db_session, [])
        assert result == {}

    async def test_unknown_for_watch_with_no_audit_events(self, db_session):
        watch = Watch(name="W", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        result = await get_watch_health_map(db_session, [watch.id])
        assert result[watch.id] is None

    async def test_returns_latest_check_event_type(self, db_session):
        watch = Watch(name="W", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        older = AuditLog(
            event_type=EventType.CHECK_FETCH_FAILED,
            watch_id=watch.id,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
        )
        newer = AuditLog(
            event_type=EventType.CHECK_NO_CHANGE,
            watch_id=watch.id,
            created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        )
        db_session.add_all([older, newer])
        await db_session.flush()

        result = await get_watch_health_map(db_session, [watch.id])
        assert result[watch.id] == EventType.CHECK_NO_CHANGE

    async def test_ignores_non_check_events(self, db_session):
        watch = Watch(name="W", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        # Only a non-check event; should still return None for check events
        db_session.add(
            AuditLog(
                event_type=EventType.WATCH_UPDATED,
                watch_id=watch.id,
            )
        )
        await db_session.flush()

        result = await get_watch_health_map(db_session, [watch.id])
        assert result[watch.id] is None

    async def test_handles_multiple_watches(self, db_session):
        w1 = Watch(name="W1", url="https://a.com", content_type="html")
        w2 = Watch(name="W2", url="https://b.com", content_type="html")
        db_session.add_all([w1, w2])
        await db_session.flush()

        db_session.add(AuditLog(event_type=EventType.CHECK_FETCH_FAILED, watch_id=w1.id))
        db_session.add(AuditLog(event_type=EventType.CHECK_SNAPSHOT_CREATED, watch_id=w2.id))
        await db_session.flush()

        result = await get_watch_health_map(db_session, [w1.id, w2.id])
        assert result[w1.id] == EventType.CHECK_FETCH_FAILED
        assert result[w2.id] == EventType.CHECK_SNAPSHOT_CREATED


@pytest.mark.integration
class TestGetWatchTimeline:
    """Tests for the unified lifecycle event timeline."""

    _snap_kwargs = dict(
        content_hash="a" * 64,
        simhash=0,
        storage_path="/tmp/s",
        text_path="/tmp/t",
        chunk_count=1,
        text_bytes=100,
        fetch_duration_ms=50,
        fetcher_used="http",
    )

    async def test_empty_watch_returns_empty(self, db_session):
        watch = Watch(name="Empty", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        assert result == []

    async def test_invalid_ulid_returns_empty(self, db_session):
        result = await get_watch_timeline(db_session, "not-a-ulid", offset=0, limit=50)
        assert result == []

    async def test_audit_log_event_surfaces_as_config_entry(self, db_session):
        watch = Watch(name="Config Watch", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        entry = AuditLog(
            event_type=EventType.WATCH_CREATED,
            watch_id=watch.id,
            payload={"name": "Config Watch"},
        )
        db_session.add(entry)
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        assert len(result) == 1
        item = result[0]
        assert item["category"] == "config"
        assert item["event_type"] == EventType.WATCH_CREATED
        assert item["timestamp"] is not None
        assert isinstance(item["summary"], str)
        assert len(item["summary"]) > 0

    async def test_snapshot_surfaces_as_run_entry(self, db_session):
        watch = Watch(name="Run Watch", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        snap = Snapshot(watch_id=watch.id, **self._snap_kwargs)
        db_session.add(snap)
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        run_entries = [r for r in result if r["category"] == "run"]
        assert len(run_entries) == 1
        assert run_entries[0]["event_type"] == "check.snapshot_created"
        assert run_entries[0]["timestamp"] is not None

    async def test_change_surfaces_as_change_entry(self, db_session):
        watch = Watch(name="Change Watch", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        prev_snap = Snapshot(watch_id=watch.id, **self._snap_kwargs)
        curr_snap = Snapshot(watch_id=watch.id, **self._snap_kwargs)
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

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        change_entries = [r for r in result if r["category"] == "change"]
        assert len(change_entries) == 1
        assert change_entries[0]["event_type"] == "change.detected"
        assert change_entries[0]["detail_url"] is not None
        assert str(change.id) in change_entries[0]["detail_url"]

    async def test_fetch_failed_audit_event_surfaces_as_error(self, db_session):
        watch = Watch(name="Error Watch", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        entry = AuditLog(
            event_type=EventType.CHECK_FETCH_FAILED,
            watch_id=watch.id,
            payload={"error": "timeout"},
        )
        db_session.add(entry)
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        error_entries = [r for r in result if r["category"] == "error"]
        assert len(error_entries) == 1

    async def test_ordering_newest_first(self, db_session):
        watch = Watch(name="Order Watch", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        t1 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        t2 = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)

        snap1 = Snapshot(watch_id=watch.id, fetched_at=t1, **self._snap_kwargs)
        snap2 = Snapshot(watch_id=watch.id, fetched_at=t2, **self._snap_kwargs)
        db_session.add_all([snap1, snap2])
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        timestamps = [r["timestamp"] for r in result]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_pagination_offset_and_limit(self, db_session):
        watch = Watch(name="Page Watch", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        for _ in range(5):
            db_session.add(Snapshot(watch_id=watch.id, **self._snap_kwargs))
        await db_session.flush()

        all_results = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        page1 = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=3)
        page2 = await get_watch_timeline(db_session, str(watch.id), offset=3, limit=3)

        assert len(page1) == 3
        assert len(page2) == 2
        # Combined pages should match the full result
        assert [r["timestamp"] for r in page1 + page2] == [r["timestamp"] for r in all_results]

    async def test_entry_keys_present(self, db_session):
        watch = Watch(name="Key Watch", url="https://a.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        snap = Snapshot(watch_id=watch.id, **self._snap_kwargs)
        db_session.add(snap)
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        assert len(result) == 1
        item = result[0]
        assert "event_type" in item
        assert "timestamp" in item
        assert "summary" in item
        assert "detail_url" in item
        assert "category" in item
