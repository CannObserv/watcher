"""Tests for `check_watched_item` and `schedule_tick` (Task 8, #160).

The per-Watch `check_watch` is gone; `check_watched_item` is the new periodic
task.  `schedule_tick` enqueues one job per WatchedItem, keyed on the
WatchedItem's own `last_checked_at`.

Since the Phase-4 cutover (#241 step 5) `check_watched_item` no longer fetches:
it issues a ``content.fetch`` command and returns. Everything downstream of the
fetch — pipeline, health, ``last_checked_at``, media-type seeding, check audits
— now happens in the apply path and is covered by ``test_fetch_apply.py``. What
remains here is the part that is still this task's: the short-circuit guards,
plus `schedule_tick`'s due-calculation (post-actions are gone since #375).
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import src.workers.tasks as tasks_mod
from src.core.models.audit_log import AuditLog
from src.core.models.domain import Domain
from src.core.models.fetch_command import FetchCommand
from src.core.validators import CONDITIONAL_GET_ENV, validator_source_key
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


async def _run_tick(db_session, monkeypatch, now) -> tuple[list, AsyncMock]:
    """Run one ``schedule_tick`` at ``now``; return (deferred calls, republish mock).

    ``raising=False``: since #375 ``schedule_tick`` no longer imports
    ``defer_status_republish`` at all, and the mock must still be there to
    prove nothing republishes.
    """
    monkeypatch.setattr(tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session))
    defer_calls: list = []
    mock_configure = MagicMock()
    mock_configure.return_value.defer_async = AsyncMock(
        side_effect=lambda **kw: defer_calls.append(kw)
    )
    monkeypatch.setattr(check_watched_item, "configure", mock_configure)
    republish = AsyncMock()
    monkeypatch.setattr(tasks_mod, "defer_status_republish", republish, raising=False)

    with patch("src.workers.tasks.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await schedule_tick(int(now.timestamp()))
    return defer_calls, republish


# ---------------------------------------------------------------------------
# check_watched_item — guards only (the fetch itself is Replicator's since #241)
# ---------------------------------------------------------------------------


class TestCheckWatchedItemGuards:
    """Every short-circuit must return before a command is issued.

    The guards are what keeps a paused / domain-suspended / url-less
    item from putting load on the bus and the origin. Post-cutover the observable
    is "no fetch_commands row", not "the fetcher was not called".
    """

    async def _run(self, db_session, monkeypatch, watched_item):
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        return await check_watched_item(str(watched_item.id))

    async def test_skips_inactive_watched_item(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, is_active=False)
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0

    async def test_skips_paused_watched_item(self, db_session, monkeypatch):
        """#188 CR-2: paused (is_active=False) is a normal state."""
        wi = await make_watched_item(db_session, is_active=True)
        wi.is_active = False
        wi.effective_url = "https://example.com/page"
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0

    async def test_skips_domain_suspended_watched_item(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session)
        wi.domain_suspended = True
        wi.effective_url = "https://example.com/page"
        await db_session.commit()

        result = await self._run(db_session, monkeypatch, wi)

        assert result.get("skipped") is True
        assert await _command_count(db_session) == 0

    async def test_skips_watched_item_without_effective_url(self, db_session, monkeypatch):
        # The column is NOT NULL with a "" default, so "unset" is the empty
        # string — that is the state a never-resolved item is actually in.
        wi = await make_watched_item(db_session)
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

    NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)

    async def test_enqueues_when_watched_item_overdue(self, db_session, monkeypatch):
        """A WatchedItem whose last_checked_at is older than the interval is due."""
        # Announced cadence = 1h (the only tier since F2, #375).
        wi = await make_watched_item(db_session)
        wi.announced_schedule_config = {"interval": "1h"}
        wi.last_checked_at = self.NOW - timedelta(hours=2)
        await db_session.commit()

        defer_calls, _ = await _run_tick(db_session, monkeypatch, self.NOW)
        assert [c["watched_item_id"] for c in defer_calls] == [str(wi.id)]

    async def test_does_not_enqueue_when_watched_item_fresh(self, db_session, monkeypatch):
        """If the WatchedItem was checked recently, it is not due."""
        wi = await make_watched_item(db_session)
        wi.announced_schedule_config = {"interval": "1h"}
        wi.last_checked_at = self.NOW - timedelta(minutes=5)
        await db_session.commit()

        defer_calls, _ = await _run_tick(db_session, monkeypatch, self.NOW)
        assert defer_calls == []

    async def test_null_last_checked_at_is_due_immediately(self, db_session, monkeypatch):
        """A WatchedItem with NULL last_checked_at is always overdue."""
        wi = await make_watched_item(db_session)
        wi.announced_schedule_config = {"interval": "1h"}
        # explicitly leave wi.last_checked_at as None.
        await db_session.commit()

        defer_calls, _ = await _run_tick(db_session, monkeypatch, self.NOW)
        assert [c["watched_item_id"] for c in defer_calls] == [str(wi.id)]

    async def test_skips_inactive_watched_items(self, db_session, monkeypatch):
        """is_active=False excludes a WatchedItem."""
        wi_inactive = await make_watched_item(
            db_session, primary_url="https://inactive.example.com"
        )
        wi_inactive.is_active = False
        await db_session.commit()

        defer_calls, _ = await _run_tick(db_session, monkeypatch, self.NOW)
        assert defer_calls == []


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
            domain_name="paused.com",
            is_active=True,
            domain_suspended=True,
        )
        await db_session.commit()

        defer_calls, _ = await _run_tick(db_session, monkeypatch, now)
        assert defer_calls == []


class TestTheTickWritesNothing:
    """#375: the tick runs no profiles and no post-actions — Archiver does
    (CannObserv/archiver#365) — so a due item is deferred and nothing written."""

    async def test_a_due_item_is_deferred_and_nothing_written(self, db_session, monkeypatch):
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        wi = await make_watched_item(db_session)
        wi.announced_schedule_config = {"interval": "1h"}
        wi.last_checked_at = now - timedelta(hours=2)
        await db_session.commit()
        audits_before = (
            await db_session.execute(select(func.count()).select_from(AuditLog))
        ).scalar_one()

        defer_calls, republish = await _run_tick(db_session, monkeypatch, now)

        await db_session.refresh(wi)
        assert wi.is_active is True
        audits_after = (
            await db_session.execute(select(func.count()).select_from(AuditLog))
        ).scalar_one()
        assert audits_after == audits_before
        republish.assert_not_awaited()
        assert [c["watched_item_id"] for c in defer_calls] == [str(wi.id)]


class TestForcedFullFetch:
    """#269 rule 5: the operator's "check now" is always a real re-read.

    The escape hatch that makes conditional GET debuggable — an operator who
    suspects a stale validator forces one full fetch from the dashboard instead
    of editing the database.
    """

    async def _issue(self, db_session, monkeypatch, wi, **kwargs):
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
        )
        await check_watched_item(str(wi.id), bus_client=AsyncMock(), **kwargs)
        return (
            await db_session.execute(
                select(FetchCommand).where(FetchCommand.watched_item_id == wi.id)
            )
        ).scalar_one()

    async def _item(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.etag = 'W/"v2"'
        wi.last_full_fetch_at = datetime.now(UTC) - timedelta(hours=1)
        wi.blob_expires_at = wi.last_full_fetch_at + timedelta(days=7)
        wi.validator_source_key = validator_source_key(
            effective_url=wi.effective_url,
            source_specs=wi.source_specs,
            generation=wi.processor_version,
        )
        await db_session.commit()
        return wi

    async def test_check_now_issues_an_unconditional_command(self, db_session, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item(db_session)

        row = await self._issue(db_session, monkeypatch, wi, force_full_fetch=True)

        assert row.request_etag is None

    async def test_a_scheduled_check_still_replays(self, db_session, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item(db_session)

        row = await self._issue(db_session, monkeypatch, wi)

        assert row.request_etag == 'W/"v2"'
