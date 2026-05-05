"""Outbox column round-trip tests for the Change model."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.models.change import Change
from tests.conftest import make_snapshot, make_watch

pytestmark = pytest.mark.integration

_SNAP_DEFAULTS = {
    "content_hash": "aabbccdd",
    "simhash": 1234,
    "storage_path": "/data/snap.raw",
    "text_path": "/data/snap.txt",
    "chunk_count": 0,
    "text_bytes": 100,
    "fetch_duration_ms": 50,
}


@pytest.mark.asyncio
async def test_outbox_columns_default_null(db_session, make_change):
    """Freshly-inserted Change has published_to_bus_at=None and bus_message_id=None."""
    watch = await make_watch(db_session)
    snap1 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    snap2 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    change = await make_change(watch, snap2, previous_snapshot=snap1)
    await db_session.commit()

    fetched = (await db_session.execute(select(Change).where(Change.id == change.id))).scalar_one()
    assert fetched.published_to_bus_at is None
    assert fetched.bus_message_id is None


@pytest.mark.asyncio
async def test_outbox_columns_round_trip(db_session, make_change):
    """Explicitly-set outbox fields persist and read back correctly."""
    watch = await make_watch(db_session)
    snap1 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    snap2 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    change = await make_change(
        watch,
        snap2,
        previous_snapshot=snap1,
        published_to_bus_at=datetime(2026, 5, 4, tzinfo=UTC),
        bus_message_id="1700000000000-0",
    )
    await db_session.commit()

    fetched = (await db_session.execute(select(Change).where(Change.id == change.id))).scalar_one()
    assert fetched.published_to_bus_at == datetime(2026, 5, 4, tzinfo=UTC)
    assert fetched.bus_message_id == "1700000000000-0"
