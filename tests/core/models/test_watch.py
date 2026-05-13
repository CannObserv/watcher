"""Round-trip tests for the Watch model's InfoItem linkage column."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.models.watch import ContentType, Watch
from tests.conftest import make_info_item, make_info_source

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_watch_accepts_info_item_id(db_session):
    """Watch persists info_item_id with cross-schema FK to information.info_items."""
    info_item = await make_info_item(db_session, name="Test")

    watch = Watch(
        name="Test",
        content_type=ContentType.HTML,
        info_item_id=info_item.info_item_id,
    )
    db_session.add(watch)
    await db_session.flush()

    assert watch.info_item_id == info_item.info_item_id

    fetched = (await db_session.execute(select(Watch).where(Watch.id == watch.id))).scalar_one()
    assert fetched.info_item_id == info_item.info_item_id


@pytest.mark.asyncio
async def test_watch_info_item_id_required(db_session):
    """Watch.info_item_id is now NOT NULL — inserting without it raises IntegrityError."""
    watch = Watch(name="Test", content_type=ContentType.HTML)
    db_session.add(watch)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_watch_accepts_info_source_id(db_session):
    """Watch persists info_source_id alongside info_item_id (transitional)."""
    info_item = await make_info_item(db_session, name="T")
    info_source = await make_info_source(db_session, url="https://example.com/t")
    watch = Watch(
        name="T",
        content_type=ContentType.HTML,
        info_item_id=info_item.info_item_id,
        info_source_id=info_source.info_source_id,
    )
    db_session.add(watch)
    await db_session.flush()
    fetched = (await db_session.execute(select(Watch).where(Watch.id == watch.id))).scalar_one()
    assert fetched.info_source_id == info_source.info_source_id
