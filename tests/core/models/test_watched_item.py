"""Tests for the WatchedItem model."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models import WatchedItem

pytestmark = pytest.mark.integration


async def test_watched_item_defaults(db_session: AsyncSession) -> None:
    archiver_info_item_id = ULID()
    wi = WatchedItem(
        archiver_info_source_id=str(ULID()),
        archiver_info_item_id=archiver_info_item_id,
    )
    db_session.add(wi)
    await db_session.flush()

    stmt = select(WatchedItem).where(WatchedItem.archiver_info_item_id == archiver_info_item_id)
    fetched = (await db_session.execute(stmt)).scalar_one()
    assert fetched.is_active is True
    assert fetched.last_checked_at is None
    assert fetched.content_media_type is None
    assert fetched.announced_schedule_config is None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_watched_item_requires_archiver_info_item_id(db_session: AsyncSession) -> None:
    """#251: the Archiver InfoItem link is NOT NULL — bare-URL rows are gone."""
    db_session.add(WatchedItem(archiver_info_source_id=str(ULID())))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_watched_item_requires_archiver_info_source_id(db_session: AsyncSession) -> None:
    """#251: the InfoSource link is NOT NULL — the drain can no longer be handed
    a revision it has nowhere to post."""
    db_session.add(WatchedItem(archiver_info_item_id=ULID()))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_watched_item_archiver_info_item_id_unique(db_session: AsyncSession) -> None:
    archiver_info_item_id = ULID()
    db_session.add(
        WatchedItem(
            archiver_info_source_id=str(ULID()),
            archiver_info_item_id=archiver_info_item_id,
        )
    )
    await db_session.flush()
    db_session.add(
        WatchedItem(
            archiver_info_source_id=str(ULID()),
            archiver_info_item_id=archiver_info_item_id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_watched_item_content_media_type_stores_raw_mime(db_session: AsyncSession) -> None:
    """Free-form raw MIME stored verbatim (#168). The essence is no longer stored —
    it's a computed field on WatchedItemResponse (see the schema tests)."""
    wi = WatchedItem(
        archiver_info_source_id=str(ULID()),
        archiver_info_item_id=ULID(),
        content_media_type="text/HTML; charset=utf-8",
    )
    db_session.add(wi)
    await db_session.flush()
    await db_session.refresh(wi)
    assert wi.content_media_type == "text/HTML; charset=utf-8"


async def test_watched_item_content_media_type_none_allowed(db_session: AsyncSession) -> None:
    wi = WatchedItem(
        archiver_info_source_id=str(ULID()),
        archiver_info_item_id=ULID(),
        content_media_type=None,
    )
    db_session.add(wi)
    await db_session.flush()
    await db_session.refresh(wi)
    assert wi.content_media_type is None
