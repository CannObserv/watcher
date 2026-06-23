"""Integration tests for dashboard context queries."""

from datetime import UTC, datetime

import pytest

from src.core.models.domain import Domain
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watched_item import WatchedItem
from src.dashboard.context import (
    get_active_profiles_by_item,
    get_dashboard_stats,
    get_domain_watched_items,
    get_domains_with_watched_item_counts,
    get_queue_health,
    get_rate_limiter_state,
)
from tests.conftest import make_info_item, make_watched_item


@pytest.mark.integration
class TestGetDashboardStats:
    async def test_empty_database(self, db_session):
        stats = await get_dashboard_stats(db_session)
        assert stats["total_watches"] == 0
        assert stats["active_watches"] == 0
        assert stats["changes_today"] == 0
        assert stats["checks_today"] == 0

    async def test_counts_watched_items(self, db_session):
        await make_watched_item(
            db_session, name="W1", primary_url="https://a.com", content_media_type="text/html"
        )
        await make_watched_item(
            db_session,
            name="W2",
            primary_url="https://b.com",
            content_media_type="text/html",
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
        _ = limiter._domains["example.com"]
        domains = get_rate_limiter_state(limiter)
        assert isinstance(domains, list)
        assert len(domains) == 1
        assert domains[0]["name"] == "example.com"
        assert domains[0]["in_backoff"] is False


@pytest.mark.integration
class TestGetDomainsWithWatchedItemCounts:
    async def test_empty_domains(self, db_session):
        result = await get_domains_with_watched_item_counts(db_session)
        assert result == []

    async def test_domain_with_watched_items(self, db_session):
        domain = Domain(name="example.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        await make_watched_item(
            db_session,
            name="Test",
            primary_url="https://example.com",
            content_media_type="text/html",
            domain_name="example.com",
        )

        result = await get_domains_with_watched_item_counts(db_session)
        assert len(result) == 1
        assert result[0]["name"] == "example.com"
        assert result[0]["watched_item_count"] == 1
        assert result[0]["in_backoff"] is False

    async def test_single_watched_item_on_domain_counts_as_one(self, db_session):
        """A domain with one WatchedItem reports watched_item_count=1."""
        domain = Domain(name="multi.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        await make_watched_item(
            db_session,
            name="Item A",
            primary_url="https://multi.com",
            content_media_type="text/html",
            domain_name="multi.com",
        )

        result = await get_domains_with_watched_item_counts(db_session)
        assert len(result) == 1
        assert result[0]["watched_item_count"] == 1

    async def test_domain_with_no_watched_items(self, db_session):
        domain = Domain(name="orphan.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        await db_session.flush()

        result = await get_domains_with_watched_item_counts(db_session)
        assert len(result) == 1
        assert result[0]["watched_item_count"] == 0

    async def test_domain_in_backoff(self, db_session):
        domain = Domain(name="slow.com", min_interval=1.0, max_concurrency=2, current_interval=4.0)
        db_session.add(domain)
        await db_session.flush()

        result = await get_domains_with_watched_item_counts(db_session)
        assert result[0]["in_backoff"] is True
        assert result[0]["current_interval"] == 4.0

    async def test_archived_watched_item_excluded_from_count(self, db_session):
        """Archived items are retired — they must not inflate the live count (#209)."""
        db_session.add(Domain(name="mixed.com", min_interval=1.0, max_concurrency=2))
        await make_watched_item(
            db_session,
            name="Live",
            primary_url="https://mixed.com/live",
            content_media_type="text/html",
            domain_name="mixed.com",
        )
        await make_watched_item(
            db_session,
            name="Gone",
            primary_url="https://mixed.com/gone",
            content_media_type="text/html",
            domain_name="mixed.com",
            archived_at=datetime.now(UTC),
        )

        result = await get_domains_with_watched_item_counts(db_session)
        assert len(result) == 1
        assert result[0]["watched_item_count"] == 1

    async def test_domain_with_only_archived_items_still_appears_with_zero(self, db_session):
        """A domain whose only item is archived stays in the list with count 0 (#209).

        Guards the LEFT-JOIN-with-ON-filter requirement: a WHERE filter would drop
        the row entirely.
        """
        db_session.add(Domain(name="retired.com", min_interval=1.0, max_concurrency=2))
        await make_watched_item(
            db_session,
            name="Gone",
            primary_url="https://retired.com",
            content_media_type="text/html",
            domain_name="retired.com",
            archived_at=datetime.now(UTC),
        )

        result = await get_domains_with_watched_item_counts(db_session)
        names = [d["name"] for d in result]
        assert "retired.com" in names
        assert next(d for d in result if d["name"] == "retired.com")["watched_item_count"] == 0


@pytest.mark.integration
class TestGetDomainsFiltered:
    async def test_search_by_name(self, db_session):
        db_session.add(Domain(name="alpha.com"))
        db_session.add(Domain(name="beta.com"))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session, search="alpha")
        assert len(result) == 1
        assert result[0]["name"] == "alpha.com"

    async def test_filter_active_excludes_archived(self, db_session):
        db_session.add(Domain(name="active.com"))
        db_session.add(Domain(name="gone.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session, status="active")
        names = [d["name"] for d in result]
        assert "active.com" in names
        assert "gone.com" not in names

    async def test_filter_archived(self, db_session):
        db_session.add(Domain(name="live.com"))
        db_session.add(Domain(name="gone.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session, status="archived")
        names = [d["name"] for d in result]
        assert "gone.com" in names
        assert "live.com" not in names

    async def test_filter_backoff(self, db_session):
        db_session.add(Domain(name="normal.com"))
        db_session.add(Domain(name="slow.com", current_interval=5.0))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session, status="backoff")
        names = [d["name"] for d in result]
        assert "slow.com" in names
        assert "normal.com" not in names

    async def test_pagination(self, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"dom{i:02d}.com"))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session, page=1, page_size=2)
        assert len(result) == 2
        assert result[0]["name"] == "dom00.com"

    async def test_pagination_page_2(self, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"dom{i:02d}.com"))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session, page=2, page_size=2)
        assert len(result) == 2
        assert result[0]["name"] == "dom02.com"

    async def test_last_checked_from_watched_item(self, db_session):
        domain = Domain(name="checked.com")
        db_session.add(domain)
        now = datetime.now(UTC)
        wi = await make_watched_item(
            db_session,
            name="W",
            primary_url="https://checked.com",
            content_media_type="text/html",
            domain_name="checked.com",
        )
        wi.last_checked_at = now
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session)
        assert result[0]["last_checked"] == now

    async def test_last_checked_excludes_archived(self, db_session):
        """An archived item's check time must not win the max (#209)."""
        db_session.add(Domain(name="freshness.com"))
        live_time = datetime(2026, 6, 1, tzinfo=UTC)
        archived_time = datetime(2026, 6, 20, tzinfo=UTC)  # newer, but archived
        live = await make_watched_item(
            db_session,
            name="Live",
            primary_url="https://freshness.com/live",
            content_media_type="text/html",
            domain_name="freshness.com",
        )
        live.last_checked_at = live_time
        archived = await make_watched_item(
            db_session,
            name="Gone",
            primary_url="https://freshness.com/gone",
            content_media_type="text/html",
            domain_name="freshness.com",
            archived_at=datetime.now(UTC),
        )
        archived.last_checked_at = archived_time
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session)
        assert result[0]["last_checked"] == live_time

    async def test_last_checked_none_when_no_watched_items(self, db_session):
        db_session.add(Domain(name="orphan.com"))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session)
        assert result[0]["last_checked"] is None

    async def test_result_includes_status(self, db_session):
        db_session.add(Domain(name="s.com"))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session)
        assert result[0]["status"] == "active"

    async def test_result_includes_notes(self, db_session):
        db_session.add(Domain(name="n.com", notes="important"))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session)
        assert result[0]["notes"] == "important"


@pytest.mark.integration
class TestGetDomainWatchedItems:
    async def test_returns_watched_items_for_domain(self, db_session):
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        db_session.add(Domain(name="other.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Ex Item",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Other Item",
                domain_name="other.com",
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
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Alpha Item",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Beta Item",
                domain_name="ex.com",
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", search="alp")
        assert len(result) == 1
        assert result[0].name == "Alpha Item"

    async def test_sort_by_name_desc(self, db_session):
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Alpha",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Beta",
                domain_name="ex.com",
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", sort="name", order="desc")
        assert [wi.name for wi in result] == ["Beta", "Alpha"]

    async def test_sort_by_last_checked_at_desc_nulls_last(self, db_session):
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Checked",
                domain_name="ex.com",
                last_checked_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Unchecked",
                domain_name="ex.com",
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(
            db_session, "ex.com", sort="last_checked_at", order="desc"
        )
        assert result[0].name == "Checked"
        assert result[1].name == "Unchecked"

    async def test_sort_by_last_checked_at_asc_nulls_first(self, db_session):
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Checked",
                domain_name="ex.com",
                last_checked_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Unchecked",
                domain_name="ex.com",
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(
            db_session, "ex.com", sort="last_checked_at", order="asc"
        )
        assert result[0].name == "Unchecked"
        assert result[1].name == "Checked"

    async def test_status_active_excludes_archived_suspended_and_inactive(self, db_session):
        items = [await make_info_item(db_session) for _ in range(4)]
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=items[0].info_item_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=items[1].info_item_id,
                name="Archived",
                domain_name="ex.com",
                archived_at=datetime(2025, 1, 1, tzinfo=UTC),
                is_active=False,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=items[2].info_item_id,
                name="Suspended",
                domain_name="ex.com",
                domain_suspended=True,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=items[3].info_item_id,
                name="Inactive",
                domain_name="ex.com",
                is_active=False,
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", status="active")
        assert [wi.name for wi in result] == ["Active"]

    async def test_status_archived_returns_only_archived(self, db_session):
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Archived",
                domain_name="ex.com",
                archived_at=datetime(2025, 1, 1, tzinfo=UTC),
                is_active=False,
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", status="archived")
        assert [wi.name for wi in result] == ["Archived"]

    async def test_status_suspended_returns_only_suspended(self, db_session):
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Suspended",
                domain_name="ex.com",
                domain_suspended=True,
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", status="suspended")
        assert [wi.name for wi in result] == ["Suspended"]

    async def test_status_inactive_returns_only_inactive(self, db_session):
        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        item_c = await make_info_item(db_session)
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_a.info_item_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_b.info_item_id,
                name="Inactive",
                domain_name="ex.com",
                is_active=False,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item_c.info_item_id,
                name="Archived",
                domain_name="ex.com",
                archived_at=datetime(2025, 1, 1, tzinfo=UTC),
                is_active=False,
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", status="inactive")
        assert [wi.name for wi in result] == ["Inactive"]

    async def test_status_none_includes_all(self, db_session):
        items = [await make_info_item(db_session) for _ in range(3)]
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_item_id=items[0].info_item_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=items[1].info_item_id,
                name="Archived",
                domain_name="ex.com",
                archived_at=datetime(2025, 1, 1, tzinfo=UTC),
                is_active=False,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_item_id=items[2].info_item_id,
                name="Suspended",
                domain_name="ex.com",
                domain_suspended=True,
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", status=None)
        assert len(result) == 3


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
                WatchedItem(archiver_info_item_id=item_a.info_item_id, name="Active"),
                WatchedItem(
                    archiver_info_item_id=item_b.info_item_id,
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
                archiver_info_item_id=item.info_item_id,
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
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="X")
        db_session.add(wi)
        await db_session.flush()
        loaded = await get_watched_item_detail(db_session, str(wi.id))
        assert loaded is not None
        assert loaded.name == "X"

    async def test_unknown_returns_none(self, db_session):
        from ulid import ULID

        from src.dashboard.context import get_watched_item_detail

        assert await get_watched_item_detail(db_session, str(ULID())) is None


@pytest.mark.integration
class TestGetActiveProfilesByItem:
    """#206 CR-5: the batch loader feeding resolve_schedule_display(profiles=…)."""

    async def _add_profile(self, db_session, wi_id, *, interval="1h", is_active=True):
        db_session.add(
            TemporalProfile(
                watched_item_id=wi_id,
                profile_type=ProfileType.EVENT,
                reference_date=datetime.now(UTC).date(),
                rules=[{"days_before": 30, "interval": interval}],
                post_action=PostAction.DEACTIVATE,
                is_active=is_active,
            )
        )

    async def test_empty_ids_returns_empty_map(self, db_session):
        assert await get_active_profiles_by_item(db_session, []) == {}

    async def test_keys_by_item_id_as_resolution_dicts(self, db_session):
        wi = await make_watched_item(db_session, name="HasProfile", primary_url="https://a.com")
        await self._add_profile(db_session, wi.id, interval="1h")
        await db_session.flush()

        result = await get_active_profiles_by_item(db_session, [wi.id])

        assert set(result) == {str(wi.id)}
        (profile_dict,) = result[str(wi.id)]
        assert profile_dict["rules"] == [{"days_before": 30, "interval": "1h"}]
        assert profile_dict["is_active"] is True  # resolution-dict shape

    async def test_inactive_profiles_excluded(self, db_session):
        wi = await make_watched_item(
            db_session, name="InactiveProfile", primary_url="https://b.com"
        )
        await self._add_profile(db_session, wi.id, is_active=False)
        await db_session.flush()

        result = await get_active_profiles_by_item(db_session, [wi.id])

        assert result == {}  # is_active filter mirrors schedule_tick

    async def test_item_without_profile_absent_from_map(self, db_session):
        wi = await make_watched_item(db_session, name="NoProfile", primary_url="https://c.com")
        await db_session.flush()

        result = await get_active_profiles_by_item(db_session, [wi.id])

        assert str(wi.id) not in result
