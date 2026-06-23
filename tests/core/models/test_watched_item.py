"""Tests for WatchedItem + the unified NotificationTemplate model (#200)."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.media_type import media_type_essence_of
from src.core.models import (
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
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
    assert fetched.content_media_type is None
    assert fetched.media_type_essence is None
    assert fetched.default_tags is None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_watched_item_domain_default_schedule_config_defaults_null(
    db_session: AsyncSession,
) -> None:
    """#205: domain_default_schedule_config defaults to None and persists as SQL NULL."""
    wi = WatchedItem(archiver_info_item_id=ULID(), name="DomDefaultNull")
    db_session.add(wi)
    await db_session.flush()

    # Round-trips as None, and `IS NULL` matches (none_as_null=True, not JSONB 'null').
    found = (
        await db_session.execute(
            select(WatchedItem).where(
                WatchedItem.id == wi.id,
                WatchedItem.domain_default_schedule_config.is_(None),
            )
        )
    ).scalar_one()
    assert found.domain_default_schedule_config is None


async def test_watched_item_domain_default_schedule_config_round_trips(
    db_session: AsyncSession,
) -> None:
    """#205: a denormalized domain default round-trips intact."""
    wi = WatchedItem(
        archiver_info_item_id=ULID(),
        name="DomDefault",
        domain_default_schedule_config={"interval": "7d"},
    )
    db_session.add(wi)
    await db_session.flush()
    fetched = (
        await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi.id))
    ).scalar_one()
    assert fetched.domain_default_schedule_config == {"interval": "7d"}


async def test_watched_item_archiver_info_item_id_unique(db_session: AsyncSession) -> None:
    archiver_info_item_id = ULID()
    db_session.add(WatchedItem(archiver_info_item_id=archiver_info_item_id, name="A"))
    await db_session.flush()
    db_session.add(WatchedItem(archiver_info_item_id=archiver_info_item_id, name="B"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_watched_item_content_media_type_stores_raw_mime(db_session: AsyncSession) -> None:
    """Free-form raw MIME stored verbatim; the essence projection strips params (#168)."""
    wi = WatchedItem(
        archiver_info_item_id=ULID(),
        name="Test",
        content_media_type="text/HTML; charset=utf-8",
    )
    db_session.add(wi)
    await db_session.flush()
    await db_session.refresh(wi)
    assert wi.content_media_type == "text/HTML; charset=utf-8"
    # Generated column: lowercased type/subtype, params stripped.
    assert wi.media_type_essence == "text/html"


async def test_watched_item_content_media_type_none_allowed(db_session: AsyncSession) -> None:
    wi = WatchedItem(archiver_info_item_id=ULID(), name="NullCT", content_media_type=None)
    db_session.add(wi)
    await db_session.flush()
    await db_session.refresh(wi)
    assert wi.content_media_type is None
    assert wi.media_type_essence is None


@pytest.mark.parametrize(
    "raw",
    [
        "text/html",
        "Text/HTML; charset=utf-8",
        "application/pdf",
        "TEXT/CSV",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet; foo=bar",
        "  application/json  ",
        None,
    ],
)
async def test_media_type_essence_python_sql_parity(db_session: AsyncSession, raw) -> None:
    """The Python media_type_essence_of mirror must agree with the DB-computed
    generated column for every input (#168 — guards the dual source of truth)."""
    wi = WatchedItem(archiver_info_item_id=ULID(), name="Parity", content_media_type=raw)
    db_session.add(wi)
    await db_session.flush()
    await db_session.refresh(wi)
    assert wi.media_type_essence == media_type_essence_of(raw)


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
