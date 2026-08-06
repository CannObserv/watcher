"""Tests for the issue path and the pending-publish sweep (#241).

``check_watched_item`` issues a ``content.fetch`` command — it is the whole of
what the task does now. These pin that the check itself has not happened at
issue time, the cheap open-command gate, and the crash-recovery sweep (MUST-2's
second half: a committed-but-unpublished row is republished under the SAME
command_id, made idempotent by Replicator's dedupe).
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import src.workers.tasks as tasks_mod
from src.core.fetch_commands import create_fetch_command
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.workers.fetch_commands import publish_pending_fetch_commands
from src.workers.tasks import check_watched_item
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 6, 16, 30, 0, tzinfo=UTC)


def _mock_session_factory(db_session: AsyncSession):
    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


def _wire(db_session, monkeypatch):
    monkeypatch.setattr(tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session))


class TestCheckWatchedItemIssue:
    async def test_issues_a_command_instead_of_fetching(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await check_watched_item(str(wi.id), bus_client=client)

        assert result["issued"]
        assert await client.xlen(streams.CONTENT_FETCH) == 1
        # The check itself has not happened yet — that's the apply path's job.
        await db_session.refresh(wi)
        assert wi.last_checked_at is None

    async def test_open_command_gates_reissue(self, db_session, monkeypatch):
        # A silently failed command must not turn schedule_tick into an
        # every-minute origin hammer — one open command per item, ever.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        first = await check_watched_item(str(wi.id), bus_client=client)
        second = await check_watched_item(str(wi.id), bus_client=client)

        assert first["issued"]
        assert second == {"skipped": True, "reason": "command_in_flight"}
        assert await client.xlen(streams.CONTENT_FETCH) == 1

    async def test_publish_failure_leaves_row_pending_for_the_sweep(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        _wire(db_session, monkeypatch)
        broken = MagicMock()
        broken.xadd = AsyncMock(side_effect=ConnectionError("broker down"))

        result = await check_watched_item(str(wi.id), bus_client=broken)

        assert result["issued"]
        assert result["published"] is False
        rows = (await db_session.execute(select(FetchCommand))).scalars().all()
        assert [r.status for r in rows] == [FetchCommandStatus.PENDING_PUBLISH]


class TestPublishPendingSweep:
    async def test_republishes_same_command_id(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        client = fakeredis.FakeAsyncRedis()

        result = await publish_pending_fetch_commands(session=db_session, bus_client=client)

        assert result == {"published": 1}
        assert row.status == FetchCommandStatus.IN_FLIGHT
        entries = await client.xrange(streams.CONTENT_FETCH)
        assert len(entries) == 1
        fields = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in entries[0][1].items()
        }
        assert fields["key"] == row.command_id  # same id — Replicator dedupes replays

    async def test_failed_publish_keeps_row_pending(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        broken = MagicMock()
        broken.xadd = AsyncMock(side_effect=ConnectionError("broker down"))

        result = await publish_pending_fetch_commands(session=db_session, bus_client=broken)

        assert result == {"published": 0}
        assert row.status == FetchCommandStatus.PENDING_PUBLISH
