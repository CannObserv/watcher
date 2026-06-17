"""Drain worker for pending_archiver_sync."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.base import generate_ulid
from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.workers.source_revisions_drain import drain_pending_archiver_sync
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

FP = "sha256:" + "a" * 64
ARCHIVER_SOURCE_ID = "01HZZ00000000000000000000S"


def _async_session_factory_returning(db_session: AsyncSession):
    """Return a fake session-factory that yields the given test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


async def _setup_pending_row(db_session: AsyncSession, *, with_archiver_id: bool = True) -> tuple:
    """Create WatchedItem + ChangeRevision + PendingArchiverSync."""
    now = datetime.now(UTC)
    wi = await make_watched_item(db_session, name="DrainTest")
    if with_archiver_id:
        wi.archiver_info_source_id = ARCHIVER_SOURCE_ID
    await db_session.flush()

    rev = ChangeRevision(
        watched_item_id=wi.id,
        content_fingerprint=FP,
        captured_at=now,
        content_size_bytes=1024,
        schema_version=1,
    )
    db_session.add(rev)
    await db_session.flush()

    pending = PendingArchiverSync(
        change_revision_id=rev.id,
        watched_item_id=wi.id,
        content_cache_uri="file:///x.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
        next_attempt_at=now,
    )
    db_session.add(pending)
    await db_session.commit()

    return wi, rev, pending


@pytest.mark.asyncio
async def test_drain_success_back_populates_revision_id(db_session, monkeypatch):
    """Successful POST: ChangeRevision.archiver_revision_id set, pending row deleted."""
    _, rev, pending = await _setup_pending_row(db_session)

    canonical_id = str(generate_ulid())
    fake_client = MagicMock()
    fake_client.post_source_revision = AsyncMock(
        return_value=MagicMock(source_revision_id=canonical_id)
    )

    from src.workers import source_revisions_drain as mod

    monkeypatch.setattr(
        mod, "get_session_factory", lambda: _async_session_factory_returning(db_session)
    )
    monkeypatch.setattr(mod, "_get_archiver_client", lambda: fake_client)

    result = await drain_pending_archiver_sync(batch_size=10)
    assert result["drained"] == 1
    assert result["failed"] == 0

    fake_client.post_source_revision.assert_awaited_once()
    call_kw = fake_client.post_source_revision.call_args.kwargs
    assert call_kw["info_source_id"] == ARCHIVER_SOURCE_ID
    assert call_kw["source_revision_id"] == str(rev.id)
    assert call_kw["content_fingerprint"] == FP

    # ChangeRevision should have archiver_revision_id back-populated.
    await db_session.refresh(rev)
    assert str(rev.archiver_revision_id) == canonical_id

    # PendingArchiverSync row should be deleted.
    remaining = (
        await db_session.execute(
            select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_drain_back_populates_revision_id_as_ulid(db_session, monkeypatch):
    """Back-populated archiver_revision_id is a ULID, matching the column type.

    The sweeper's cache-clear keys on this attribute (#194); a raw str would
    diverge from the Mapped[ULID] declaration and the round-trip the sweeper
    relies on.
    """
    _, rev, _ = await _setup_pending_row(db_session)

    canonical_id = str(generate_ulid())
    fake_client = MagicMock()
    fake_client.post_source_revision = AsyncMock(
        return_value=MagicMock(source_revision_id=canonical_id)
    )

    from src.workers import source_revisions_drain as mod

    monkeypatch.setattr(
        mod, "get_session_factory", lambda: _async_session_factory_returning(db_session)
    )
    monkeypatch.setattr(mod, "_get_archiver_client", lambda: fake_client)

    await drain_pending_archiver_sync(batch_size=10)

    # In-memory attribute is a ULID (the column type), not a raw str.
    assert isinstance(rev.archiver_revision_id, ULID)
    assert str(rev.archiver_revision_id) == canonical_id


@pytest.mark.asyncio
async def test_drain_marks_failure_on_archiver_error(db_session, monkeypatch):
    """ConnectError → row.attempts++, last_error set, row remains."""
    _, _, pending = await _setup_pending_row(db_session)
    now = datetime.now(UTC)

    fake_client = MagicMock()
    fake_client.post_source_revision = AsyncMock(side_effect=httpx.ConnectError("nope"))

    from src.workers import source_revisions_drain as mod

    monkeypatch.setattr(
        mod, "get_session_factory", lambda: _async_session_factory_returning(db_session)
    )
    monkeypatch.setattr(mod, "_get_archiver_client", lambda: fake_client)

    result = await drain_pending_archiver_sync(batch_size=10)
    assert result["drained"] == 0
    assert result["failed"] == 1

    stored = (
        await db_session.execute(
            select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
        )
    ).scalar_one()
    assert stored.attempts == 1
    assert stored.last_error and "ConnectError" in stored.last_error
    assert stored.next_attempt_at > now


@pytest.mark.asyncio
async def test_drain_drops_row_when_archiver_info_source_id_missing(
    db_session, monkeypatch, caplog
):
    """Row dropped (logged + deleted) when WatchedItem has no archiver_info_source_id."""
    _, _, pending = await _setup_pending_row(db_session, with_archiver_id=False)

    fake_client = MagicMock()
    fake_client.post_source_revision = AsyncMock()

    from src.workers import source_revisions_drain as mod

    monkeypatch.setattr(
        mod, "get_session_factory", lambda: _async_session_factory_returning(db_session)
    )
    monkeypatch.setattr(mod, "_get_archiver_client", lambda: fake_client)

    with caplog.at_level("ERROR", logger="src.workers.source_revisions_drain"):
        await drain_pending_archiver_sync(batch_size=10)

    fake_client.post_source_revision.assert_not_awaited()
    remaining = (
        await db_session.execute(
            select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
        )
    ).scalar_one_or_none()
    assert remaining is None
    assert any("archiver_info_source_id" in r.message for r in caplog.records)
