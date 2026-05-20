"""Tests for WatchedItem + WatchedItemNotificationTemplate models."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models import ContentType, WatchedItem, WatchedItemNotificationTemplate

pytestmark = pytest.mark.integration


async def test_watched_item_defaults(db_session: AsyncSession) -> None:
    info_item_id = ULID()
    wi = WatchedItem(info_item_id=info_item_id, name="Test WatchedItem")
    db_session.add(wi)
    await db_session.flush()

    stmt = select(WatchedItem).where(WatchedItem.info_item_id == info_item_id)
    fetched = (await db_session.execute(stmt)).scalar_one()
    assert fetched.name == "Test WatchedItem"
    assert fetched.is_active is True
    assert fetched.archived_at is None
    assert fetched.last_reviewed_at is None
    assert fetched.last_checked_at is None
    assert fetched.default_schedule_config is None
    assert fetched.default_content_type is None
    assert fetched.default_tags is None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_watched_item_info_item_id_unique(db_session: AsyncSession) -> None:
    info_item_id = ULID()
    db_session.add(WatchedItem(info_item_id=info_item_id, name="A"))
    await db_session.flush()
    db_session.add(WatchedItem(info_item_id=info_item_id, name="B"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_watched_item_validates_default_content_type(db_session: AsyncSession) -> None:
    wi = WatchedItem(
        info_item_id=ULID(),
        name="Test",
        default_content_type="html",  # string coerces to enum
    )
    assert wi.default_content_type == ContentType.HTML

    with pytest.raises(ValueError, match="Invalid default_content_type"):
        WatchedItem(info_item_id=ULID(), name="Bad", default_content_type="not_a_type")


async def test_watched_item_default_content_type_none_allowed(db_session: AsyncSession) -> None:
    wi = WatchedItem(info_item_id=ULID(), name="NullCT", default_content_type=None)
    assert wi.default_content_type is None


async def test_notification_template_defaults(db_session: AsyncSession) -> None:
    wi = WatchedItem(info_item_id=ULID(), name="W")
    db_session.add(wi)
    await db_session.flush()

    tmpl = WatchedItemNotificationTemplate(
        watched_item_id=wi.id,
        channel_hint="slack",
        remote_channel_id="01ABCDEFGHJKMNPQRSTVWXYZ12",
    )
    db_session.add(tmpl)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(WatchedItemNotificationTemplate).where(
                WatchedItemNotificationTemplate.watched_item_id == wi.id
            )
        )
    ).scalar_one()
    assert fetched.channel_hint == "slack"
    assert fetched.is_active is True
    assert fetched.events == ["change_detected"]
    assert fetched.title is None
    assert fetched.content_config is None


async def test_notification_template_cascade_delete(db_session: AsyncSession) -> None:
    wi = WatchedItem(info_item_id=ULID(), name="W")
    db_session.add(wi)
    await db_session.flush()
    tmpl_id = ULID()
    tmpl = WatchedItemNotificationTemplate(
        id=tmpl_id,
        watched_item_id=wi.id,
        channel_hint="mailto",
    )
    db_session.add(tmpl)
    await db_session.flush()

    await db_session.delete(wi)
    await db_session.flush()

    result = await db_session.execute(
        select(WatchedItemNotificationTemplate).where(WatchedItemNotificationTemplate.id == tmpl_id)
    )
    assert result.scalar_one_or_none() is None
