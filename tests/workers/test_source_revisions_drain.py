"""Drain worker for pending_source_revisions."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.pending_source_revision import PendingSourceRevision
from src.workers.source_revisions_drain import drain_pending_source_revisions

pytestmark = pytest.mark.integration

FP = "sha256:" + "a" * 64


def _async_session_factory_returning(db_session: AsyncSession):
    """Return a fake session-factory that yields the given test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


@pytest.mark.asyncio
async def test_drain_posts_and_deletes_on_success(db_session, monkeypatch):
    """Successful POST → row deleted; dispatch_event_notifications called with source_revision_id."""  # noqa: E501
    now = datetime.now(UTC)
    row = PendingSourceRevision(
        info_source_id="01HZZ00000000000000000000F",
        content_fingerprint=FP,
        captured_at=now,
        content_cache_uri="file:///x.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
        next_attempt_at=now,
    )
    db_session.add(row)
    await db_session.commit()

    fake_client = MagicMock()
    fake_client.post_source_revision = AsyncMock(
        return_value=MagicMock(
            source_revision_id=str(row.id),
            content_fingerprint=FP,
        )
    )
    fake_dispatch = AsyncMock()

    from src.workers import source_revisions_drain as mod

    monkeypatch.setattr(
        mod,
        "get_session_factory",
        lambda: _async_session_factory_returning(db_session),
    )
    monkeypatch.setattr(mod, "_get_archiver_client", lambda: fake_client)
    monkeypatch.setattr(mod, "dispatch_event_notifications", fake_dispatch)

    result = await drain_pending_source_revisions(batch_size=10)
    assert result["drained"] == 1
    assert result["failed"] == 0
    fake_client.post_source_revision.assert_awaited_once()
    # TODO(#156): Once Watch.info_source_id lands (Task 5.1), seed a real Watch
    # in the success-path test and assert dispatch IS awaited.
    fake_dispatch.assert_not_awaited()

    # Row must be deleted after success.
    remaining = (
        await db_session.execute(
            select(PendingSourceRevision).where(PendingSourceRevision.id == row.id)
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_drain_marks_failure_on_archiver_error(db_session, monkeypatch):
    """ConnectError → row.attempts++, last_error set, row remains."""
    now = datetime.now(UTC)
    row = PendingSourceRevision(
        info_source_id="01HZZ00000000000000000000F",
        content_fingerprint=FP,
        captured_at=now,
        content_cache_uri="file:///x.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
        next_attempt_at=now,
    )
    db_session.add(row)
    await db_session.commit()

    fake_client = MagicMock()
    fake_client.post_source_revision = AsyncMock(side_effect=httpx.ConnectError("nope"))

    from src.workers import source_revisions_drain as mod

    monkeypatch.setattr(mod, "_get_archiver_client", lambda: fake_client)
    monkeypatch.setattr(mod, "dispatch_event_notifications", AsyncMock())
    monkeypatch.setattr(
        mod,
        "get_session_factory",
        lambda: _async_session_factory_returning(db_session),
    )

    result = await drain_pending_source_revisions(batch_size=10)
    assert result["drained"] == 0
    assert result["failed"] == 1

    stored = (
        await db_session.execute(
            select(PendingSourceRevision).where(PendingSourceRevision.id == row.id)
        )
    ).scalar_one()
    assert stored.attempts == 1
    assert stored.last_error and "ConnectError" in stored.last_error
    assert stored.next_attempt_at > now
