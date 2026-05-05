"""Round-trip tests for the Change model's InfoItem linkage columns."""

import pytest
from sqlalchemy import select

from src.core.models.change import Change
from tests.conftest import make_info_item, make_snapshot, make_watch

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
async def test_info_columns_round_trip(db_session, make_change):
    """info_item_id, info_spec_id, previous/current_fingerprint persist round-trip."""
    watch = await make_watch(db_session)
    info_item = await make_info_item(db_session, name="Linked Item")
    snap1 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    snap2 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    spec_id = info_item.info_item_id  # opaque ULID stand-in for an InfoSpec id

    change = await make_change(
        watch,
        snap2,
        previous_snapshot=snap1,
        info_item_id=info_item.info_item_id,
        info_spec_id=spec_id,
        previous_fingerprint=1111111111,
        current_fingerprint=2222222222,
    )
    await db_session.commit()

    fetched = (await db_session.execute(select(Change).where(Change.id == change.id))).scalar_one()
    assert fetched.info_item_id == info_item.info_item_id
    assert fetched.info_spec_id == spec_id
    assert fetched.previous_fingerprint == 1111111111
    assert fetched.current_fingerprint == 2222222222


@pytest.mark.asyncio
async def test_info_columns_default_null(db_session, make_change):
    """Freshly inserted Change has all four info columns null when unspecified."""
    watch = await make_watch(db_session)
    snap1 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    snap2 = await make_snapshot(db_session, watch, **_SNAP_DEFAULTS)
    change = await make_change(watch, snap2, previous_snapshot=snap1)
    await db_session.commit()

    fetched = (await db_session.execute(select(Change).where(Change.id == change.id))).scalar_one()
    assert fetched.info_item_id is None
    assert fetched.info_spec_id is None
    assert fetched.previous_fingerprint is None
    assert fetched.current_fingerprint is None
