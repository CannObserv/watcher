"""Integration tests for dashboard context queries."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.change_revision import ChangeRevision
from src.core.models.domain import Domain
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watched_item import WatchedItem
from src.dashboard.context import (
    get_active_profiles_by_item,
    get_audit_entries_count,
    get_dashboard_stats,
    get_distinct_audit_event_types,
    get_domain_watched_items,
    get_domains_with_watched_item_counts,
    get_queue_health,
)
from tests.conftest import make_watched_item
from tests.dashboard.conftest import (
    backdate_job_events,
    defer_job,
    defer_periodic_job,
    finish_job,
    retry_job,
    run_job_to_success,
    start_job,
)


@pytest.mark.integration
class TestGetDashboardStats:
    async def test_empty_database(self, db_session):
        stats = await get_dashboard_stats(db_session)
        assert stats["total_items"] == 0
        assert stats["active_items"] == 0
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
        assert stats["total_items"] == 2
        assert stats["active_items"] == 1

    async def test_changes_today_counts_todays_change_revisions(self, db_session):
        """changes_today is backed by ChangeRevision rows captured today (#229)."""
        wi = await make_watched_item(db_session)
        await db_session.flush()
        db_session.add(
            ChangeRevision(
                watched_item_id=wi.id,
                content_fingerprint="fp-today",
                captured_at=datetime.now(UTC),
                schema_version=1,
            )
        )
        db_session.add(
            ChangeRevision(
                watched_item_id=wi.id,
                content_fingerprint="fp-yesterday",
                captured_at=datetime.now(UTC) - timedelta(days=1),
                schema_version=1,
            )
        )
        await db_session.flush()
        stats = await get_dashboard_stats(db_session)
        assert stats["changes_today"] == 1

    async def test_changes_today_excludes_baseline_revisions(self, db_session):
        """An item's first-ever revision is a baseline, not a change (CR round 1).

        The pipeline creates a ChangeRevision on the first successful check to
        establish the fingerprint baseline — provisioning a new item must not
        show up as a 'change today' on the dashboard.
        """
        wi = await make_watched_item(db_session)
        await db_session.flush()
        db_session.add(
            ChangeRevision(
                watched_item_id=wi.id,
                content_fingerprint="fp-baseline",
                captured_at=datetime.now(UTC),
                schema_version=1,
            )
        )
        await db_session.flush()
        stats = await get_dashboard_stats(db_session)
        assert stats["changes_today"] == 0


def _utc_midnight() -> datetime:
    """The instant ``get_queue_health`` opens its window on."""
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.integration
class TestGetQueueHealth:
    async def test_returns_queue_stats(self, db_session):
        queue = await get_queue_health(db_session)
        assert "todo" in queue
        assert "doing" in queue
        assert "failed" in queue
        assert "succeeded_today" in queue

    async def test_the_procrastinate_tables_are_really_present(self, procrastinate_session):
        """The premise of every test below.

        ``get_queue_health`` returns zeros when the schema is missing, and a
        test that reads those zeros passes against any query — which is how the
        ``scheduled_at`` filter went unnoticed (#298).
        """
        for table in ("procrastinate_jobs", "procrastinate_events"):
            regclass = await procrastinate_session.scalar(
                text("SELECT to_regclass(:table)"), {"table": table}
            )
            assert regclass == table

    async def test_counts_a_first_try_success(self, procrastinate_session):
        """A plain defer leaves ``scheduled_at`` NULL, so a filter on it counted
        none of these — the healthier the queue, the closer to zero the tile
        read (#298)."""
        job_id = await run_job_to_success(procrastinate_session)

        scheduled_at = await procrastinate_session.scalar(
            text("SELECT scheduled_at FROM procrastinate_jobs WHERE id = :job_id"),
            {"job_id": job_id},
        )
        assert scheduled_at is None

        queue = await get_queue_health(procrastinate_session)
        assert queue["succeeded_today"] == 1

    async def test_counts_a_periodic_success(self, procrastinate_session):
        """Watcher's queue is filled by cron ticks, and a periodic defer leaves
        ``scheduled_at`` NULL too (#298)."""
        job_id = await defer_periodic_job(procrastinate_session)
        await start_job(procrastinate_session, job_id)
        await finish_job(procrastinate_session, job_id)

        scheduled_at = await procrastinate_session.scalar(
            text("SELECT scheduled_at FROM procrastinate_jobs WHERE id = :job_id"),
            {"job_id": job_id},
        )
        assert scheduled_at is None

        queue = await get_queue_health(procrastinate_session)
        assert queue["succeeded_today"] == 1

    async def test_counts_a_success_after_a_retry(self, procrastinate_session):
        """The retry is the one path that sets ``scheduled_at`` — the only case
        the old query got right, and it must stay counted."""
        job_id = await defer_job(procrastinate_session)
        await start_job(procrastinate_session, job_id)
        await retry_job(procrastinate_session, job_id, retry_at=datetime.now(UTC))
        await start_job(procrastinate_session, job_id)
        await finish_job(procrastinate_session, job_id)

        queue = await get_queue_health(procrastinate_session)
        assert queue["succeeded_today"] == 1

    async def test_counts_both_defer_paths_together(self, procrastinate_session):
        """Two successes today, one of each shape: the tile reads 2."""
        await run_job_to_success(procrastinate_session)
        retried = await defer_job(procrastinate_session)
        await start_job(procrastinate_session, retried)
        await retry_job(procrastinate_session, retried, retry_at=datetime.now(UTC))
        await start_job(procrastinate_session, retried)
        await finish_job(procrastinate_session, retried)

        queue = await get_queue_health(procrastinate_session)
        assert queue["succeeded_today"] == 2

    async def test_a_success_a_microsecond_before_utc_midnight_is_not_today(
        self, procrastinate_session
    ):
        """The window opens at UTC midnight, which the tile's label names.

        A microsecond out, not a day: a query counting a rolling 24 hours, or
        one opening at the host's local midnight, passes the looser version.
        """
        job_id = await run_job_to_success(procrastinate_session)
        await backdate_job_events(
            procrastinate_session, job_id, at=_utc_midnight() - timedelta(microseconds=1)
        )

        queue = await get_queue_health(procrastinate_session)
        assert queue["succeeded_today"] == 0

    async def test_a_success_exactly_at_utc_midnight_is_today(self, procrastinate_session):
        """The boundary belongs to today — the filter is ``>=``, not ``>``."""
        job_id = await run_job_to_success(procrastinate_session)
        await backdate_job_events(procrastinate_session, job_id, at=_utc_midnight())

        queue = await get_queue_health(procrastinate_session)
        assert queue["succeeded_today"] == 1

    async def test_counts_unfinished_and_failed_jobs_by_status(self, procrastinate_session):
        """The other three numbers read the jobs table, where status lives."""
        await defer_job(procrastinate_session)
        doing = await defer_job(procrastinate_session)
        await start_job(procrastinate_session, doing)
        failed = await defer_job(procrastinate_session)
        await start_job(procrastinate_session, failed)
        await finish_job(procrastinate_session, failed, status="failed")

        queue = await get_queue_health(procrastinate_session)
        assert queue["todo"] == 1
        assert queue["doing"] == 1
        assert queue["failed"] == 1
        # Today's failure is an event too, on the same table the tile now counts.
        assert queue["succeeded_today"] == 0


@pytest.mark.integration
class TestGetDomainsWithWatchedItemCounts:
    async def test_empty_domains(self, db_session):
        result = await get_domains_with_watched_item_counts(db_session)
        assert result == []

    async def test_domain_with_watched_items(self, db_session):
        domain = Domain(name="example.com", min_interval=1.0)
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

    async def test_single_watched_item_on_domain_counts_as_one(self, db_session):
        """A domain with one WatchedItem reports watched_item_count=1."""
        domain = Domain(name="multi.com", min_interval=1.0)
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
        domain = Domain(name="orphan.com", min_interval=1.0)
        db_session.add(domain)
        await db_session.flush()

        result = await get_domains_with_watched_item_counts(db_session)
        assert len(result) == 1
        assert result[0]["watched_item_count"] == 0

    async def test_archived_watched_item_excluded_from_count(self, db_session):
        """Archived items are retired — they must not inflate the live count (#209)."""
        db_session.add(Domain(name="mixed.com", min_interval=1.0))
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
        db_session.add(Domain(name="retired.com", min_interval=1.0))
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

    async def test_unknown_status_filter_returns_everything(self, db_session):
        """The retired "backoff" segment must not silently filter to nothing."""
        db_session.add(Domain(name="normal.com"))
        db_session.add(Domain(name="slow.com"))
        await db_session.flush()
        result = await get_domains_with_watched_item_counts(db_session, status="backoff")
        names = [d["name"] for d in result]
        assert "slow.com" in names
        assert "normal.com" in names

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
        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add(Domain(name="ex.com"))
        db_session.add(Domain(name="other.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Ex Item",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
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
        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Alpha Item",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
                name="Beta Item",
                domain_name="ex.com",
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", search="alp")
        assert len(result) == 1
        assert result[0].name == "Alpha Item"

    async def test_sort_by_name_desc(self, db_session):
        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Alpha",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
                name="Beta",
                domain_name="ex.com",
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", sort="name", order="desc")
        assert [wi.name for wi in result] == ["Beta", "Alpha"]

    async def test_sort_by_last_checked_at_desc_nulls_last(self, db_session):
        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Checked",
                domain_name="ex.com",
                last_checked_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
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
        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Checked",
                domain_name="ex.com",
                last_checked_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
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
        item_ids = [ULID() for _ in range(4)]
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_ids[0],
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_ids[1],
                name="Archived",
                domain_name="ex.com",
                archived_at=datetime(2025, 1, 1, tzinfo=UTC),
                is_active=False,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_ids[2],
                name="Suspended",
                domain_name="ex.com",
                domain_suspended=True,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_ids[3],
                name="Inactive",
                domain_name="ex.com",
                is_active=False,
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", status="active")
        assert [wi.name for wi in result] == ["Active"]

    async def test_status_archived_returns_only_archived(self, db_session):
        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
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
        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
                name="Suspended",
                domain_name="ex.com",
                domain_suspended=True,
            )
        )
        await db_session.flush()
        result = await get_domain_watched_items(db_session, "ex.com", status="suspended")
        assert [wi.name for wi in result] == ["Suspended"]

    async def test_status_inactive_returns_only_inactive(self, db_session):
        item_a_id = ULID()
        item_b_id = ULID()
        item_c_id = ULID()
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_a_id,
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_b_id,
                name="Inactive",
                domain_name="ex.com",
                is_active=False,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_c_id,
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
        item_ids = [ULID() for _ in range(3)]
        db_session.add(Domain(name="ex.com"))
        await db_session.flush()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_ids[0],
                name="Active",
                domain_name="ex.com",
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_ids[1],
                name="Archived",
                domain_name="ex.com",
                archived_at=datetime(2025, 1, 1, tzinfo=UTC),
                is_active=False,
            )
        )
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_ids[2],
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

        item_a_id = ULID()
        item_b_id = ULID()
        db_session.add_all(
            [
                WatchedItem(
                    archiver_info_source_id=str(ULID()),
                    archiver_info_item_id=item_a_id,
                    name="Active",
                ),
                WatchedItem(
                    archiver_info_source_id=str(ULID()),
                    archiver_info_item_id=item_b_id,
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

        item_id = ULID()
        db_session.add(
            WatchedItem(
                archiver_info_source_id=str(ULID()),
                archiver_info_item_id=item_id,
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

        item_id = ULID()
        wi = WatchedItem(
            archiver_info_source_id=str(ULID()), archiver_info_item_id=item_id, name="X"
        )
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


@pytest.mark.integration
class TestGetAuditEntriesCount:
    """Pager totals for the audit-log / recent-activity table (#215)."""

    async def test_empty_returns_zero(self, db_session):
        assert await get_audit_entries_count(db_session) == 0

    async def test_counts_all_without_filters(self, db_session):
        db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        db_session.add(AuditLog(event_type=EventType.WATCHED_ITEM_CREATED, payload={}))
        await db_session.flush()
        assert await get_audit_entries_count(db_session) == 2

    async def test_filters_by_event_type(self, db_session):
        db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        db_session.add(AuditLog(event_type=EventType.CHECK_FETCH_FAILED, payload={}))
        await db_session.flush()
        count = await get_audit_entries_count(db_session, event_types=[EventType.CHECK_NO_CHANGE])
        assert count == 1

    async def test_event_types_are_or_matched(self, db_session):
        """Multiple event_types union their counts (#215 bug — OR, not AND)."""
        db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        db_session.add(AuditLog(event_type=EventType.CHECK_FETCH_FAILED, payload={}))
        db_session.add(AuditLog(event_type=EventType.WATCHED_ITEM_CREATED, payload={}))
        await db_session.flush()
        count = await get_audit_entries_count(
            db_session,
            event_types=[EventType.CHECK_NO_CHANGE, EventType.CHECK_FETCH_FAILED],
        )
        assert count == 2

    async def test_filters_by_watched_item_id(self, db_session):
        wi = await make_watched_item(db_session, name="Counted", primary_url="https://a.com")
        db_session.add(
            AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={"watched_item_id": str(wi.id)})
        )
        db_session.add(
            AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={"watched_item_id": "other"})
        )
        await db_session.flush()
        count = await get_audit_entries_count(db_session, watched_item_id=str(wi.id))
        assert count == 1


@pytest.mark.integration
class TestGetDistinctAuditEventTypes:
    """Dynamic chip source for the Audit Log filter (#217)."""

    async def test_empty_returns_empty(self, db_session):
        assert await get_distinct_audit_event_types(db_session) == []

    async def test_returns_distinct_sorted(self, db_session):
        for et in (
            EventType.CHECK_NO_CHANGE,
            EventType.CHECK_NO_CHANGE,  # duplicate collapses
            EventType.WATCHED_ITEM_PAUSED,
            EventType.CHECK_FETCH_FAILED,
        ):
            db_session.add(AuditLog(event_type=et, payload={}))
        await db_session.flush()
        result = await get_distinct_audit_event_types(db_session)
        # distinct, alphabetical (prefix-grouped)
        assert result == [
            "check.fetch_failed",
            "check.no_change",
            "watched_item.paused",
        ]
