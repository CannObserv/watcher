"""Round-trip tests for the Watch model's InfoSource linkage column."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.models.watch import ContentType, Watch
from tests.conftest import make_info_source

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_watch_accepts_info_source_id(db_session):
    """Watch persists info_source_id with cross-schema FK to information.info_sources."""
    info_source = await make_info_source(db_session, url="https://example.com/t")

    watch = Watch(
        name="Test",
        content_type=ContentType.HTML,
        info_source_id=info_source.info_source_id,
    )
    db_session.add(watch)
    await db_session.flush()

    assert watch.info_source_id == info_source.info_source_id

    fetched = (await db_session.execute(select(Watch).where(Watch.id == watch.id))).scalar_one()
    assert fetched.info_source_id == info_source.info_source_id


@pytest.mark.asyncio
async def test_watch_info_source_id_required(db_session):
    """Inserting without info_source_id raises IntegrityError."""
    watch = Watch(name="T", content_type=ContentType.HTML)
    db_session.add(watch)
    with pytest.raises(IntegrityError):
        await db_session.flush()


def test_watch_no_longer_has_info_item_id():
    assert not hasattr(Watch, "info_item_id")
