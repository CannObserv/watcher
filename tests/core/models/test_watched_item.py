"""Tests for WatchedItem + the unified NotificationTemplate model (#200)."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models import (
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
    ContentType,
    NotificationTemplate,
    WatchedItem,
)

pytestmark = pytest.mark.integration


async def test_watched_item_defaults(db_session: AsyncSession) -> None:
    archiver_info_item_id = ULID()
    wi = WatchedItem(archiver_info_item_id=archiver_info_item_id, name="Test WatchedItem")
    db_session.add(wi)
    await db_session.flush()

    stmt = select(WatchedItem).where(WatchedItem.archiver_info_item_id == archiver_info_item_id)
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


async def test_watched_item_archiver_info_item_id_unique(db_session: AsyncSession) -> None:
    archiver_info_item_id = ULID()
    db_session.add(WatchedItem(archiver_info_item_id=archiver_info_item_id, name="A"))
    await db_session.flush()
    db_session.add(WatchedItem(archiver_info_item_id=archiver_info_item_id, name="B"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_watched_item_validates_default_content_type(db_session: AsyncSession) -> None:
    wi = WatchedItem(
        archiver_info_item_id=ULID(),
        name="Test",
        default_content_type="html",  # string coerces to enum
    )
    assert wi.default_content_type == ContentType.HTML

    with pytest.raises(ValueError, match="Invalid default_content_type"):
        WatchedItem(archiver_info_item_id=ULID(), name="Bad", default_content_type="not_a_type")


async def test_watched_item_default_content_type_none_allowed(db_session: AsyncSession) -> None:
    wi = WatchedItem(archiver_info_item_id=ULID(), name="NullCT", default_content_type=None)
    assert wi.default_content_type is None


async def test_notification_template_defaults(db_session: AsyncSession) -> None:
    """A watched_item-visibility NotificationTemplate persists and uses server defaults."""
    wi = WatchedItem(archiver_info_item_id=ULID(), name="W")
    db_session.add(wi)
    await db_session.flush()

    tmpl = NotificationTemplate(
        title="Item template",
        watched_item_id=wi.id,
        channel_hint="slack",
        remote_channel_id="01ABCDEFGHJKMNPQRSTVWXYZ12",
        visibility=VISIBILITY_WATCHED_ITEM,
    )
    db_session.add(tmpl)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(NotificationTemplate).where(NotificationTemplate.watched_item_id == wi.id)
        )
    ).scalar_one()
    assert fetched.channel_hint == "slack"
    assert fetched.is_active is True
    assert fetched.events == ["change_detected"]
    assert fetched.title == "Item template"
    assert fetched.visibility == VISIBILITY_WATCHED_ITEM
    assert fetched.domain_name is None
    assert fetched.content_config is None


async def test_notification_template_cascade_delete(db_session: AsyncSession) -> None:
    """Deleting a WatchedItem cascades to its watched_item-visibility templates."""
    wi = WatchedItem(archiver_info_item_id=ULID(), name="W")
    db_session.add(wi)
    await db_session.flush()
    tmpl_id = ULID()
    tmpl = NotificationTemplate(
        id=tmpl_id,
        title="Item template",
        watched_item_id=wi.id,
        channel_hint="mailto",
        visibility=VISIBILITY_WATCHED_ITEM,
    )
    db_session.add(tmpl)
    await db_session.flush()

    await db_session.delete(wi)
    await db_session.flush()

    result = await db_session.execute(
        select(NotificationTemplate).where(NotificationTemplate.id == tmpl_id)
    )
    assert result.scalar_one_or_none() is None


async def test_notification_template_visibility_check_rejects_mismatch(
    db_session: AsyncSession,
) -> None:
    """ck_notification_templates_visibility_refs rejects a ref/visibility mismatch (#200).

    A global-visibility template must have both refs NULL; supplying a
    watched_item_id violates the CHECK constraint.
    """
    wi = WatchedItem(archiver_info_item_id=ULID(), name="W")
    db_session.add(wi)
    await db_session.flush()

    bad = NotificationTemplate(
        title="Inconsistent",
        channel_hint="slack",
        visibility=VISIBILITY_GLOBAL,
        watched_item_id=wi.id,  # not allowed for global visibility
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()
