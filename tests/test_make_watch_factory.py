"""Tests for the test factory itself."""

import pytest

from src.core.models import WatchedItem
from tests.conftest import (
    bind_primary_source,
    make_info_item,
    make_info_source,
    make_watch,
)

pytestmark = pytest.mark.integration


async def test_make_watch_auto_creates_info_item_source_and_watched_item(db_session):
    """Factory wires up an InfoItem, primary InfoSource binding, and WatchedItem."""
    watch = await make_watch(db_session, name="Auto")
    assert watch.info_item_id is not None
    assert watch.target_info_source_id is None
    assert watch.watched_item_id is not None
    assert watch.watched_item is not None
    assert watch.watched_item.info_item_id == watch.info_item_id


async def test_make_watch_eager_loads_watched_item(db_session):
    """The factory refreshes the relationship so callers don't pay a lazy load."""
    watch = await make_watch(db_session, name="Eager")
    # No await on `watch.watched_item` access — joined-loaded by the factory.
    assert isinstance(watch.watched_item, WatchedItem)


async def test_make_watch_with_existing_info_item_attaches_to_existing_watched_item(
    db_session,
):
    """Two Watches on the same info_item_id share a WatchedItem."""
    item = await make_info_item(db_session)
    primary = await make_info_source(db_session, url="https://example.com/shared")
    await bind_primary_source(
        db_session,
        info_item_id=item.info_item_id,
        info_source_id=primary.info_source_id,
    )

    w1 = await make_watch(db_session, name="First", info_item_id=item.info_item_id)
    w2 = await make_watch(db_session, name="Second", info_item_id=item.info_item_id)
    assert w1.watched_item_id == w2.watched_item_id


async def test_make_watch_rejects_mismatched_watched_item(db_session):
    """Passing a WatchedItem bound to a different InfoItem raises."""
    item1 = await make_info_item(db_session, name="A")
    item2 = await make_info_item(db_session, name="B")
    primary1 = await make_info_source(db_session, url="https://example.com/a")
    primary2 = await make_info_source(db_session, url="https://example.com/b")
    await bind_primary_source(
        db_session,
        info_item_id=item1.info_item_id,
        info_source_id=primary1.info_source_id,
    )
    await bind_primary_source(
        db_session,
        info_item_id=item2.info_item_id,
        info_source_id=primary2.info_source_id,
    )

    wi1 = WatchedItem(info_item_id=item1.info_item_id, name="W1")
    db_session.add(wi1)
    await db_session.flush()

    with pytest.raises(AssertionError, match="must match"):
        await make_watch(
            db_session,
            name="bad",
            info_item_id=item2.info_item_id,
            watched_item=wi1,
        )
