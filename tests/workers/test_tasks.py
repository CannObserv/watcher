"""Tests for `check_watched_item` and `schedule_tick` (Task 8, #160).

The per-Watch `check_watch` is gone; `check_watched_item` is the new periodic
task.  `schedule_tick` enqueues one job per WatchedItem, keyed on the
WatchedItem's own `last_checked_at`.

Since the Phase-4 cutover (#241 step 5) `check_watched_item` no longer fetches:
it issues a ``content.fetch`` command and returns. Everything downstream of the
fetch — pipeline, health, ``last_checked_at``, media-type seeding, check audits
— now happens in the apply path and is covered by ``test_fetch_apply.py``. What
remains here is the part that is still this task's: the short-circuit guards,
plus `schedule_tick`'s due-calculation and post-actions.
"""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import src.workers.tasks as tasks_mod
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.fetch_command import FetchCommand
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.scheduling.resolution import resolved_schedule_config
from src.workers.tasks import check_watched_item, schedule_tick
from tests.conftest import make_watched_item

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


async def _command_count(db_session) -> int:
    """How many fetch commands exist — the observable of "did it issue?"."""
    return (await db_session.execute(select(func.count()).select_from(FetchCommand))).scalar_one()


# ---------------------------------------------------------------------------
# check_watched_item — guards only (the fetch itself is Replicator's since #241)
# ---------------------------------------------------------------------------


class TestCheckWatchedItemGuards:
    """Every short-circuit must return before a command is issued.

    The guards are what keeps a paused / archived / domain-suspended / url-less
    item from putting load on the bus and the origin. Post-cutover the observable
    is "no fetch_commands row", not "the fetcher was not called".
    """

    async def _run(self, db_session, monkeypatch, watched_item):
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        return await check_watched_item(str(watched_item.id))

    async def test_skips_inactive_watched_item(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, name="Inactive", is_active=False)
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0

    async def test_skips_paused_watched_item(self, db_session, monkeypatch):
        """#188 CR-2: paused (is_active=False, NOT archived) is a normal state."""
        wi = await make_watched_item(db_session, name="Paused", is_active=True)
        wi.is_active = False
        wi.effective_url = "https://example.com/page"
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0

    async def test_skips_archived_watched_item(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, name="Archived")
        wi.archived_at = datetime.now(UTC)
        wi.effective_url = "https://example.com/page"
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0

    async def test_skips_domain_suspended_watched_item(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, name="Suspended")
        wi.domain_suspended = True
        wi.effective_url = "https://example.com/page"
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0

    async def test_skips_watched_item_without_effective_url(self, db_session, monkeypatch):
        # The column is NOT NULL with a "" default, so "unset" is the empty
        # string — that is the state a never-resolved item is actually in.
        wi = await make_watched_item(db_session, name="NoUrl")
        wi.effective_url = ""
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert result.get("reason") == "no_effective_url"
        assert await _command_count(db_session) == 0

    async def test_missing_watched_item_is_a_noop(self, db_session, monkeypatch):
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        result = await check_watched_item("01ARZ3NDEKTSV4RRFFQ69G5FAV")

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0


# ---------------------------------------------------------------------------
# schedule_tick (per-WatchedItem aggregation).
# ---------------------------------------------------------------------------


class TestScheduleTickAggregation:
    """schedule_tick enqueues per-WatchedItem based on WatchedItem.last_checked_at."""

    async def test_enqueues_when_watched_item_overdue(self, db_session, monkeypatch):
        """A WatchedItem whose last_checked_at is older than the interval is due."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        # Default schedule on the WatchedItem = 1h.
        wi = await make_watched_item(db_session, name="A")
        wi.default_schedule_config = {"interval": "1h"}
        wi.last_checked_at = now - timedelta(hours=2)
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

    async def test_does_not_enqueue_when_watched_item_fresh(self, db_session, monkeypatch):
        """If the WatchedItem was checked recently, it is not due."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        wi = await make_watched_item(db_session, name="Fresh")
        wi.default_schedule_config = {"interval": "1h"}
        wi.last_checked_at = now - timedelta(minutes=5)
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
        """A WatchedItem with NULL last_checked_at is always overdue."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        wi = await make_watched_item(db_session, name="Never")
        wi.default_schedule_config = {"interval": "1h"}
        # explicitly leave wi.last_checked_at as None.
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
        wi_inactive = await make_watched_item(
            db_session, name="Inactive WI", primary_url="https://inactive.example.com"
        )
        wi_inactive.is_active = False
        wi_archived = await make_watched_item(
            db_session, name="Archived WI", primary_url="https://archived.example.com"
        )
        wi_archived.archived_at = now - timedelta(days=1)
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

    async def test_skips_when_watched_item_on_inactive_domain(self, db_session, monkeypatch):
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        domain = Domain(name="paused.com", is_active=False)
        db_session.add(domain)
        # #191: schedule_tick skips on WatchedItem.domain_suspended (the flag the
        # domain-deactivation cascade sets), not via a live Domain join.
        await make_watched_item(
            db_session,
            name="On Paused Domain",
            domain_name="paused.com",
            is_active=True,
            domain_suspended=True,
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
# Post-actions: reduce_frequency mutates the WatchedItem's default schedule.
# ---------------------------------------------------------------------------


class TestPostActions:
    """reduce_frequency must mutate the WatchedItem's default schedule."""

    async def test_reduce_frequency_mutates_watched_item_default(self, db_session, monkeypatch):
        """post_action=reduce_frequency slows the WatchedItem's default schedule."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)

        # WatchedItem starts at 1h interval.
        wi = await make_watched_item(db_session, name="Profiled")
        wi.default_schedule_config = {"interval": "1h"}
        wi.last_checked_at = now - timedelta(hours=25)

        # Expired event profile attached to the WatchedItem — reduce_frequency.
        profile = TemporalProfile(
            watched_item_id=wi.id,
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

        # The WatchedItem now resolves to the 1d interval.
        assert resolved_schedule_config(wi).get("interval") == "1d"

        # Audit log: WATCHED_ITEM_THROTTLED for the WatchedItem.
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

    async def test_reduce_frequency_noop_when_cadence_already_slower_than_1d(
        self, db_session, monkeypatch
    ):
        """#205: reduce_frequency must not *speed up* an item slower than 1d.

        An item inheriting a 7d domain cadence (no own interval) must stay at 7d —
        reduce_frequency is a no-op, the item config is left untouched (inheritance
        preserved), and no WATCHED_ITEM_THROTTLED audit is written.
        """
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)

        # Inherits a 7d domain cadence — no item-level interval.
        wi = await make_watched_item(db_session, name="SlowDomain")
        wi.default_schedule_config = None
        wi.domain_default_schedule_config = {"interval": "7d"}
        wi.last_checked_at = now - timedelta(days=8)

        profile = TemporalProfile(
            watched_item_id=wi.id,
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 5, 1),  # past → reduce_frequency fires
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

        await db_session.refresh(wi)
        # Item config untouched → still inheriting the 7d domain cadence.
        assert wi.default_schedule_config is None
        assert resolved_schedule_config(wi).get("interval") == "7d"

        # No throttle audit — nothing was slowed.
        throttle_rows = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_THROTTLED)
                )
            )
            .scalars()
            .all()
        )
        assert throttle_rows == []

    async def test_deactivate_post_action_deactivates_watched_item(self, db_session, monkeypatch):
        """deactivate flips the WatchedItem is_active off."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        wi = await make_watched_item(db_session, name="Will Deactivate")
        wi.default_schedule_config = {"interval": "1d"}
        wi.last_checked_at = now - timedelta(hours=25)

        profile = TemporalProfile(
            watched_item_id=wi.id,
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

        await db_session.refresh(wi)
        assert wi.is_active is False
        await db_session.refresh(profile)
        assert profile.is_active is False

    async def test_archive_post_action_archives_watched_item(self, db_session, monkeypatch):
        """#191: archive post-action flips is_active=False AND stamps archived_at."""
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        wi = await make_watched_item(db_session, name="Will Archive")
        wi.default_schedule_config = {"interval": "1d"}
        wi.last_checked_at = now - timedelta(hours=25)

        profile = TemporalProfile(
            watched_item_id=wi.id,
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

        await db_session.refresh(wi)
        assert wi.is_active is False
        assert wi.archived_at is not None
