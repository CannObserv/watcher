"""Integration tests for dashboard context queries."""

from datetime import UTC, datetime

import pytest

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.watch import ContentType
from src.dashboard.context import (
    get_dashboard_stats,
    get_domain_watched_items,
    get_domains_with_watch_counts,
    get_queue_health,
    get_rate_limiter_state,
    get_watch_detail,
    get_watch_list,
    get_watch_timeline,
)
from tests.conftest import make_watch


@pytest.mark.integration
class TestGetDashboardStats:
    async def test_empty_database(self, db_session):
        stats = await get_dashboard_stats(db_session)
        assert stats["total_watches"] == 0
        assert stats["active_watches"] == 0
        assert stats["changes_today"] == 0
        assert stats["checks_today"] == 0

    async def test_counts_watches(self, db_session):
        await make_watch(db_session, name="W1", primary_url="https://a.com", content_type="html")
        await make_watch(
            db_session,
            name="W2",
            primary_url="https://b.com",
            content_type="html",
            is_active=False,
        )
        await db_session.flush()
        stats = await get_dashboard_stats(db_session)
        assert stats["total_watches"] == 2
        assert stats["active_watches"] == 1


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
        await make_watch(db_session, name="W1", primary_url="https://a.com", content_type="html")

        result = await get_watch_list(db_session)
        assert len(result) == 1
        assert result[0].name == "W1"
        assert hasattr(result[0], "last_checked_at")

    async def test_filter_active(self, db_session):
        await make_watch(
            db_session, name="Active", primary_url="https://a.com", content_type="html"
        )
        await make_watch(
            db_session,
            name="Inactive",
            primary_url="https://b.com",
            content_type="html",
            is_active=False,
        )
        await db_session.flush()

        active_only = await get_watch_list(db_session, is_active=True)
        assert len(active_only) == 1
        assert active_only[0].name == "Active"

    async def test_excludes_archived_by_default(self, db_session):
        await make_watch(
            db_session, name="Normal", primary_url="https://a.com", content_type="html"
        )
        await make_watch(
            db_session,
            name="Archived",
            primary_url="https://b.com",
            content_type="html",
            is_active=False,
            is_archived=True,
        )
        await db_session.flush()

        result = await get_watch_list(db_session)
        names = [w.name for w in result]
        assert "Normal" in names
        assert "Archived" not in names

    async def test_include_archived_returns_all(self, db_session):
        await make_watch(
            db_session, name="Normal", primary_url="https://a.com", content_type="html"
        )
        await make_watch(
            db_session,
            name="Archived",
            primary_url="https://b.com",
            content_type="html",
            is_active=False,
            is_archived=True,
        )
        await db_session.flush()

        result = await get_watch_list(db_session, include_archived=True)
        names = [w.name for w in result]
        assert "Normal" in names
        assert "Archived" in names

    async def test_search_filters_by_name(self, db_session):
        await make_watch(
            db_session, name="Alpha Watch", primary_url="https://a.com", content_type="html"
        )
        await make_watch(
            db_session, name="Beta Watch", primary_url="https://b.com", content_type="html"
        )
        await db_session.flush()
        result = await get_watch_list(db_session, search="alpha")
        assert len(result) == 1
        assert result[0].name == "Alpha Watch"

    async def test_domain_filters_by_effective_domain(self, db_session):
        await make_watch(
            db_session,
            name="W1",
            primary_url="https://a.com",
            content_type="html",
            domain_name="a.com",
        )
        await make_watch(
            db_session,
            name="W2",
            primary_url="https://b.com",
            content_type="html",
            domain_name="b.com",
        )
        await db_session.flush()
        result = await get_watch_list(db_session, domain="a.com")
        assert len(result) == 1
        assert result[0].name == "W1"

    async def test_domain_filter_is_partial_match(self, db_session):
        await make_watch(
            db_session,
            name="Sub",
            primary_url="https://sub.example.com",
            content_type="html",
            domain_name="sub.example.com",
        )
        await make_watch(
            db_session,
            name="Root",
            primary_url="https://example.com",
            content_type="html",
            domain_name="example.com",
        )
        await make_watch(
            db_session,
            name="Other",
            primary_url="https://other.com",
            content_type="html",
            domain_name="other.com",
        )
        await db_session.flush()
        result = await get_watch_list(db_session, domain="example")
        names = {w.name for w in result}
        assert "Sub" in names
        assert "Root" in names
        assert "Other" not in names

    async def test_sort_by_name_asc(self, db_session):
        await make_watch(db_session, name="Zebra", primary_url="https://a.com", content_type="html")
        await make_watch(db_session, name="Apple", primary_url="https://b.com", content_type="html")
        await db_session.flush()
        result = await get_watch_list(db_session, sort="name", order="asc")
        assert result[0].name == "Apple"
        assert result[1].name == "Zebra"

    async def test_sort_by_name_desc(self, db_session):
        await make_watch(db_session, name="Zebra", primary_url="https://a.com", content_type="html")
        await make_watch(db_session, name="Apple", primary_url="https://b.com", content_type="html")
        await db_session.flush()
        result = await get_watch_list(db_session, sort="name", order="desc")
        assert result[0].name == "Zebra"

    async def test_unknown_sort_key_falls_back_to_last_checked(self, db_session):
        await make_watch(db_session, name="W", primary_url="https://a.com", content_type="html")
        await db_session.flush()
        result = await get_watch_list(db_session, sort="INVALID", order="asc")
        assert len(result) == 1

    async def test_null_last_changed_at_first_when_asc(self, db_session):
        await make_watch(
            db_session,
            name="Changed",
            primary_url="https://a.com",
            content_type="html",
            last_changed_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        await make_watch(
            db_session, name="NeverChanged", primary_url="https://b.com", content_type="html"
        )
        await db_session.flush()
        result = await get_watch_list(db_session, sort="last_changed_at", order="asc")
        assert result[0].name == "NeverChanged"

    async def test_null_last_changed_at_last_when_desc(self, db_session):
        await make_watch(
            db_session,
            name="Changed",
            primary_url="https://a.com",
            content_type="html",
            last_changed_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        await make_watch(
            db_session, name="NeverChanged", primary_url="https://b.com", content_type="html"
        )
        await db_session.flush()
        result = await get_watch_list(db_session, sort="last_changed_at", order="desc")
        assert result[-1].name == "NeverChanged"


@pytest.mark.integration
class TestGetWatchDetail:
    async def test_returns_watch(self, db_session):
        watch = await make_watch(
            db_session, name="Detail", primary_url="https://a.com", content_type="html"
        )

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
class TestGetDomainsWithWatchCounts:
    async def test_empty_domains(self, db_session):
        result = await get_domains_with_watch_counts(db_session)
        assert result == []

    async def test_domain_with_watches(self, db_session):
        domain = Domain(name="example.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        await make_watch(
            db_session,
            name="Test",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            domain_name="example.com",
        )

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
        await make_watch(
            db_session,
            name="W",
            primary_url="https://checked.com",
            content_type="html",
            domain_name="checked.com",
            last_checked_at=now,
        )
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


@pytest.mark.integration
class TestGetWatchTimeline:
    """Tests for the lifecycle event timeline (AuditLog-only after Phase 5 #156)."""

    async def test_empty_watch_returns_empty(self, db_session):
        watch = await make_watch(
            db_session, name="Empty", primary_url="https://a.com", content_type="html"
        )

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        assert result == []

    async def test_invalid_ulid_returns_empty(self, db_session):
        result = await get_watch_timeline(db_session, "not-a-ulid", offset=0, limit=50)
        assert result == []

    async def test_audit_log_event_surfaces_as_config_entry(self, db_session):
        watch = await make_watch(
            db_session, name="Config Watch", primary_url="https://a.com", content_type="html"
        )

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

    async def test_fetch_failed_audit_event_surfaces_as_error(self, db_session):
        watch = await make_watch(
            db_session, name="Error Watch", primary_url="https://a.com", content_type="html"
        )

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
        watch = await make_watch(
            db_session, name="Order Watch", primary_url="https://a.com", content_type="html"
        )

        t1 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        t2 = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)

        for ts in (t1, t2):
            entry = AuditLog(
                event_type=EventType.CHECK_NO_CHANGE,
                watch_id=watch.id,
                created_at=ts,
            )
            db_session.add(entry)
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        timestamps = [r["timestamp"] for r in result]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_pagination_offset_and_limit(self, db_session):
        watch = await make_watch(
            db_session, name="Page Watch", primary_url="https://a.com", content_type="html"
        )

        for _ in range(5):
            entry = AuditLog(event_type=EventType.CHECK_NO_CHANGE, watch_id=watch.id)
            db_session.add(entry)
        await db_session.flush()

        all_results = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        page1 = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=3)
        page2 = await get_watch_timeline(db_session, str(watch.id), offset=3, limit=3)

        assert len(page1) == 3
        assert len(page2) == 2
        assert [r["timestamp"] for r in page1 + page2] == [r["timestamp"] for r in all_results]

    async def test_entry_keys_present(self, db_session):
        watch = await make_watch(
            db_session, name="Key Watch", primary_url="https://a.com", content_type="html"
        )

        entry = AuditLog(event_type=EventType.CHECK_NO_CHANGE, watch_id=watch.id)
        db_session.add(entry)
        await db_session.flush()

        result = await get_watch_timeline(db_session, str(watch.id), offset=0, limit=50)
        assert len(result) == 1
        item = result[0]
        assert "event_type" in item
        assert "timestamp" in item
        assert "summary" in item
        assert "detail_url" in item


@pytest.mark.integration
class TestGetDomainWatchedItems:
    async def test_returns_watched_items_for_domain(self, db_session):
        from src.core.models.domain import Domain
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        db_session.add(Domain(name="other.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(info_item_id=item_a.info_item_id, name="Ex Item", domain_name="ex.com")
        )
        db_session.add(
            WatchedItem(
                info_item_id=item_b.info_item_id, name="Other Item", domain_name="other.com"
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com")
        assert len(result) == 1
        assert result[0].name == "Ex Item"

    async def test_returns_empty_for_unknown_domain(self, db_session):
        result = await get_domain_watched_items(db_session, "unknown.com")
        assert result == []

    async def test_search_filters_by_name(self, db_session):
        from src.core.models.domain import Domain
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(info_item_id=item_a.info_item_id, name="Alpha Item", domain_name="ex.com")
        )
        db_session.add(
            WatchedItem(info_item_id=item_b.info_item_id, name="Beta Item", domain_name="ex.com")
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", search="alp")
        assert len(result) == 1
        assert result[0].name == "Alpha Item"


@pytest.mark.integration
class TestGetWatchedItemList:
    async def test_excludes_archived_by_default(self, db_session):
        from src.core.models.watched_item import WatchedItem
        from src.dashboard.context import get_watched_item_list
        from tests.conftest import make_info_item

        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add_all(
            [
                WatchedItem(info_item_id=item_a.info_item_id, name="Active"),
                WatchedItem(
                    info_item_id=item_b.info_item_id,
                    name="Archived",
                    archived_at=datetime.now(UTC),
                    is_active=False,
                ),
            ]
        )
        await db_session.flush()
        results = await get_watched_item_list(db_session)
        names = [wi.name for wi in results]
        assert "Active" in names
        assert "Archived" not in names

    async def test_include_archived(self, db_session):
        from src.core.models.watched_item import WatchedItem
        from src.dashboard.context import get_watched_item_list
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        db_session.add(
            WatchedItem(
                info_item_id=item.info_item_id,
                name="Arc",
                archived_at=datetime.now(UTC),
                is_active=False,
            )
        )
        await db_session.flush()
        results = await get_watched_item_list(db_session, include_archived=True)
        assert any(wi.name == "Arc" for wi in results)


@pytest.mark.integration
class TestGetWatchedItemDetail:
    async def test_returns_record(self, db_session):
        from src.core.models.watched_item import WatchedItem
        from src.dashboard.context import get_watched_item_detail
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="X")
        db_session.add(wi)
        await db_session.flush()
        loaded = await get_watched_item_detail(db_session, str(wi.id))
        assert loaded is not None
        assert loaded.name == "X"

    async def test_unknown_returns_none(self, db_session):
        from ulid import ULID

        from src.dashboard.context import get_watched_item_detail

        assert await get_watched_item_detail(db_session, str(ULID())) is None
