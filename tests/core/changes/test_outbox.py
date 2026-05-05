"""Outbox helper tests against the real `changes` table."""

import pytest
from ulid import ULID

from src.core.changes.outbox import mark_published, select_unpublished
from tests.conftest import make_snapshot, make_watch

# Mark all tests as integration since they touch the DB
pytestmark = pytest.mark.integration


def _snapshot_kwargs(content_hash="hash1"):
    """Return standard kwargs for snapshot creation."""
    return {
        "content_hash": content_hash,
        "simhash": 123,
        "storage_path": "s/1.html",
        "text_path": "s/1.txt",
        "storage_backend": "local",
        "chunk_count": 1,
        "text_bytes": 100,
        "fetch_duration_ms": 100,
    }


@pytest.mark.asyncio
async def test_select_unpublished_returns_only_unpublished(db_session, make_change):
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs())
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    c1 = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)
    c2 = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)

    await mark_published(db_session, c1.id, bus_message_id="1-0")
    await db_session.commit()

    unpublished = await select_unpublished(db_session)
    assert len(unpublished) == 1
    assert unpublished[0].id == c2.id


@pytest.mark.asyncio
async def test_select_unpublished_orders_by_detected_at(db_session, make_change):
    """Older unpublished rows come first."""
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs())
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    c_old = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)
    c_new = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)

    unpublished = await select_unpublished(db_session)
    assert [c.id for c in unpublished] == [c_old.id, c_new.id]


@pytest.mark.asyncio
async def test_mark_published_sets_fields(db_session, make_change):
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs())
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    c = await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)

    await mark_published(db_session, c.id, bus_message_id="abc-0")
    await db_session.commit()
    await db_session.refresh(c)
    assert c.bus_message_id == "abc-0"
    assert c.published_to_bus_at is not None


@pytest.mark.asyncio
async def test_mark_published_unknown_id_is_noop(db_session):
    # Should not raise.
    await mark_published(db_session, ULID(), bus_message_id="x")
    await db_session.commit()


@pytest.mark.asyncio
async def test_select_unpublished_respects_limit(db_session, make_change):
    watch = await make_watch(db_session)
    s1 = await make_snapshot(db_session, watch, **_snapshot_kwargs())
    s2 = await make_snapshot(db_session, watch, **_snapshot_kwargs("hash2"))
    for _ in range(5):
        await make_change(watch=watch, current_snapshot=s2, previous_snapshot=s1)

    out = await select_unpublished(db_session, limit=3)
    assert len(out) == 3
