"""Unit tests for find_info_item — substring search helper."""

import pytest

from src.information.core.models import InfoItem
from src.information.core.tools.find_info_item import find_info_item


@pytest.mark.asyncio
async def test_find_info_item_empty_query_raises(session):
    with pytest.raises(ValueError):
        await find_info_item(session, "")


@pytest.mark.asyncio
async def test_find_info_item_orders_newest_first(session):
    older = InfoItem(name="alpha-1")
    newer = InfoItem(name="alpha-2")
    session.add(older)
    await session.flush()
    session.add(newer)
    await session.flush()

    results = await find_info_item(session, "alpha")
    assert [r.name for r in results] == ["alpha-2", "alpha-1"]


@pytest.mark.asyncio
async def test_find_info_item_respects_limit(session):
    for i in range(5):
        session.add(InfoItem(name=f"beta-{i}"))
    await session.flush()

    results = await find_info_item(session, "beta", limit=3)
    assert len(results) == 3
