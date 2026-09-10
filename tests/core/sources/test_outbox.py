"""Helpers for the pending_archiver_sync outbox."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.core.models.base import generate_ulid
from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.sources.outbox import (
    clear_backoffs,
    delete_pending,
    mark_failure,
    select_due,
)
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


class TestClearBackoffs:
    """#291: a publish that succeeds is evidence about the *broker*, so every
    sibling still waiting out an exponential backoff is waiting on a condition
    that has demonstrably cleared. Without this, an operator who widens an ACL
    rule (or frees the broker's memory) waits up to the 3600 s cap before
    ``content.revisions`` resumes, and the recovery looks stuck."""

    async def test_a_failure_delayed_row_is_pulled_forward(self, db_session):
        row, _, _ = await _make_pending(db_session, offset_seconds=3600)
        row.attempts = 7
        await db_session.flush()
        before = datetime.now(UTC)

        cleared = await clear_backoffs(db_session)

        assert cleared == 1
        await db_session.refresh(row)
        assert row.next_attempt_at <= datetime.now(UTC)
        assert row.next_attempt_at >= before - timedelta(seconds=1)

    async def test_attempts_survives_the_clear(self, db_session):
        """The counter is the row's failure history and the ceiling's input;
        only the *schedule* was made stale by the broker recovering."""
        row, _, _ = await _make_pending(db_session, offset_seconds=3600)
        row.attempts = 7
        await db_session.flush()

        await clear_backoffs(db_session)

        await db_session.refresh(row)
        assert row.attempts == 7

    async def test_a_dead_lettered_row_is_left_alone(self, db_session):
        row, _, _ = await _make_pending(db_session, offset_seconds=3600)
        row.attempts = 7
        row.dead_lettered_at = datetime.now(UTC)
        await db_session.flush()
        scheduled = row.next_attempt_at

        assert await clear_backoffs(db_session) == 0

        await db_session.refresh(row)
        assert row.next_attempt_at == scheduled

    async def test_a_delay_no_failure_caused_is_left_alone(self, db_session):
        """``attempts > 0`` is the whole claim: this clears backoff, not any
        future scheduling a later feature might add."""
        row, _, _ = await _make_pending(db_session, offset_seconds=3600)
        await db_session.flush()
        scheduled = row.next_attempt_at

        assert await clear_backoffs(db_session) == 0

        await db_session.refresh(row)
        assert row.next_attempt_at == scheduled

    async def test_excluded_rows_keep_their_backoff(self, db_session):
        """Rows the caller already decided this pass. A row that just failed
        beside a success is not evidence of anything — pulling it forward would
        retry it every tick and remove the damping the backoff exists for."""
        just_failed, _, _ = await _make_pending(db_session, offset_seconds=3600)
        sibling, _, _ = await _make_pending(db_session, offset_seconds=3600)
        just_failed.attempts = sibling.attempts = 7
        await db_session.flush()
        scheduled = just_failed.next_attempt_at

        cleared = await clear_backoffs(db_session, exclude_ids=[just_failed.id])

        assert cleared == 1
        await db_session.refresh(just_failed)
        assert just_failed.next_attempt_at == scheduled

    async def test_a_second_clear_finds_nothing_to_do(self, db_session):
        """Self-limiting, and the reason a recovery costs one bulk write rather
        than one per tick: a cleared row is due, so the predicate that found it
        no longer matches (CR 13)."""
        row, _, _ = await _make_pending(db_session, offset_seconds=3600)
        row.attempts = 7
        await db_session.flush()

        assert await clear_backoffs(db_session) == 1
        assert await clear_backoffs(db_session) == 0


async def test_select_due_breaks_ties_by_id(db_session):
    """A bulk clear stamps one timestamp across many rows, so ``next_attempt_at``
    alone stops being a total order. ULIDs are time-ordered, so the id restores
    insertion order within a tie rather than leaving it to the planner (#291)."""
    first, _, _ = await _make_pending(db_session, offset_seconds=3600)
    second, _, _ = await _make_pending(db_session, offset_seconds=3600)
    third, _, _ = await _make_pending(db_session, offset_seconds=3600)
    for row in (first, second, third):
        row.attempts = 7
    await db_session.flush()
    await clear_backoffs(db_session)

    rows = await select_due(db_session, limit=10)

    ordered = [r.id for r in rows if r.id in {first.id, second.id, third.id}]
    assert ordered == sorted([first.id, second.id, third.id])
