"""Helpers for pending_source_revisions."""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.models.pending_source_revision import PendingSourceRevision
from src.core.sources.outbox import (
    delete_pending,
    enqueue_pending,
    mark_failure,
    select_due,
)

pytestmark = pytest.mark.integration

FP = "sha256:" + "a" * 64


@pytest.mark.asyncio
async def test_enqueue_pending_writes_row(db_session):
    row = await enqueue_pending(
        db_session,
        info_source_id="01HZZ00000000000000000000F",
        content_fingerprint=FP,
        captured_at=datetime.now(UTC),
        content_cache_uri="file:///x.bin",
        content_cache_expires_at=datetime.now(UTC) + timedelta(seconds=600),
    )
    assert row.id is not None
    assert row.attempts == 0
    assert row.next_attempt_at <= datetime.now(UTC) + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_select_due_excludes_future(db_session):
    now = datetime.now(UTC)
    future = await enqueue_pending(
        db_session,
        info_source_id="01HZZ00000000000000000000A",
        content_fingerprint=FP,
        captured_at=now,
        content_cache_uri="file:///a.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
    )
    future.next_attempt_at = now + timedelta(hours=1)
    due = await enqueue_pending(
        db_session,
        info_source_id="01HZZ00000000000000000000B",
        content_fingerprint=FP,
        captured_at=now,
        content_cache_uri="file:///b.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
    )
    await db_session.flush()
    rows = await select_due(db_session, limit=10)
    ids = {r.id for r in rows}
    assert due.id in ids
    assert future.id not in ids


@pytest.mark.asyncio
async def test_mark_failure_advances_backoff(db_session):
    now = datetime.now(UTC)
    row = await enqueue_pending(
        db_session,
        info_source_id="01HZZ00000000000000000000C",
        content_fingerprint=FP,
        captured_at=now,
        content_cache_uri="file:///c.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
    )
    await mark_failure(db_session, row, error="ConnectionError")
    assert row.attempts == 1
    assert row.last_error == "ConnectionError"
    assert row.next_attempt_at > now


@pytest.mark.asyncio
async def test_delete_pending_removes_row(db_session):
    from sqlalchemy import select

    now = datetime.now(UTC)
    row = await enqueue_pending(
        db_session,
        info_source_id="01HZZ00000000000000000000D",
        content_fingerprint=FP,
        captured_at=now,
        content_cache_uri="file:///d.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
    )
    await delete_pending(db_session, row.id)
    result = await db_session.execute(
        select(PendingSourceRevision).where(PendingSourceRevision.id == row.id)
    )
    assert result.scalar_one_or_none() is None
