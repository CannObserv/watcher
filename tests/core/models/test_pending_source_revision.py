"""Round-trip tests for pending_source_revisions."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.models.pending_source_revision import PendingSourceRevision

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_round_trip(db_session):
    now = datetime.now(UTC)
    row = PendingSourceRevision(
        info_source_id="01HZZ00000000000000000000F",
        content_fingerprint="sha256:" + "a" * 64,
        captured_at=now,
        content_cache_uri="file:///var/cache/watcher/scratch/01JV0000000000000000000000.bin",
        content_cache_expires_at=now + timedelta(seconds=600),
        next_attempt_at=now,
    )
    db_session.add(row)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(PendingSourceRevision).where(PendingSourceRevision.id == row.id)
        )
    ).scalar_one()
    assert fetched.attempts == 0
    assert fetched.last_error is None
    assert fetched.content_fingerprint == row.content_fingerprint
    assert fetched.captured_at == row.captured_at


@pytest.mark.asyncio
async def test_unique_source_and_fingerprint(db_session):
    now = datetime.now(UTC)
    kw = dict(
        info_source_id="01HZZ00000000000000000000F",
        content_fingerprint="sha256:" + "a" * 64,
        captured_at=now,
        content_cache_uri="file:///x.bin",
        content_cache_expires_at=now,
        next_attempt_at=now,
    )
    db_session.add(PendingSourceRevision(**kw))
    await db_session.flush()
    db_session.add(PendingSourceRevision(**kw))
    with pytest.raises(IntegrityError):
        await db_session.flush()
