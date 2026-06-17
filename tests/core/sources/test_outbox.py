"""Helpers for the pending_archiver_sync outbox."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.core.models.base import generate_ulid
from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.sources.outbox import delete_pending, mark_failure, select_due
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

FP = "sha256:" + "a" * 64


async def _make_pending(session, *, offset_seconds: int = 0) -> tuple:
    """Create WatchedItem + ChangeRevision + PendingArchiverSync."""
    now = datetime.now(UTC)
    wi = await make_watched_item(session, name=f"OutboxTest-{generate_ulid()}")

    rev = ChangeRevision(
        watched_item_id=wi.id,
        content_fingerprint=FP,
        captured_at=now,
        content_size_bytes=512,
        schema_version=1,
    )
    session.add(rev)
    await session.flush()

    pending = PendingArchiverSync(
        change_revision_id=rev.id,
        watched_item_id=wi.id,
        content_cache_uri="file:///x.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
        next_attempt_at=now + timedelta(seconds=offset_seconds),
    )
    session.add(pending)
    await session.flush()
    return pending, rev, wi


@pytest.mark.asyncio
async def test_select_due_returns_due_rows(db_session):
    pending, _, _ = await _make_pending(db_session)
    rows = await select_due(db_session, limit=10)
    ids = {r.id for r in rows}
    assert pending.id in ids


@pytest.mark.asyncio
async def test_select_due_excludes_future(db_session):
    future, _, _ = await _make_pending(db_session, offset_seconds=3600)
    due, _, _ = await _make_pending(db_session, offset_seconds=0)
    rows = await select_due(db_session, limit=10)
    ids = {r.id for r in rows}
    assert due.id in ids
    assert future.id not in ids


@pytest.mark.asyncio
async def test_mark_failure_advances_backoff(db_session):
    pending, _, _ = await _make_pending(db_session)
    now = datetime.now(UTC)
    await mark_failure(db_session, pending, error="ConnectionError: timeout")
    assert pending.attempts == 1
    assert pending.last_error == "ConnectionError: timeout"
    assert pending.next_attempt_at > now


@pytest.mark.asyncio
async def test_delete_pending_removes_row(db_session):
    pending, _, _ = await _make_pending(db_session)
    await delete_pending(db_session, pending.id)
    result = await db_session.execute(
        select(PendingArchiverSync).where(PendingArchiverSync.id == pending.id)
    )
    assert result.scalar_one_or_none() is None
