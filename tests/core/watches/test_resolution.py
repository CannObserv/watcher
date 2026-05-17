"""Tests for the resolution chain: Watch override → WatchedItem default → system default."""

from unittest.mock import MagicMock

import pytest

from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.watch import ContentType
from src.core.models.watched_item_notification_template import (
    WatchedItemNotificationTemplate,
)
from src.core.watches.resolution import (
    SYSTEM_DEFAULT_SCHEDULE_CONFIG,
    resolved_content_type,
    resolved_notification_dispatches,
    resolved_schedule_config,
    resolved_tags,
)
from tests.conftest import make_watch


def _watch(*, content_type=None, tags=None, watched_item=None):
    w = MagicMock()
    w.content_type = content_type
    w.tags = tags
    w.watched_item = watched_item
    return w


def _wi(*, default_schedule_config=None, default_content_type=None, default_tags=None):
    wi = MagicMock()
    wi.default_schedule_config = default_schedule_config
    wi.default_content_type = default_content_type
    wi.default_tags = default_tags
    return wi


def test_schedule_config_falls_back_to_system_default():
    w = _watch(watched_item=_wi(default_schedule_config=None))
    assert resolved_schedule_config(w) == SYSTEM_DEFAULT_SCHEDULE_CONFIG


def test_schedule_config_uses_watched_item_value():
    w = _watch(watched_item=_wi(default_schedule_config={"interval": "30m"}))
    assert resolved_schedule_config(w) == {"interval": "30m"}


def test_schedule_config_empty_dict_is_intentional_no_interval():
    """An empty dict on WatchedItem means 'no override' but it's set; pass through.

    `compute_next_check` tolerates a missing `interval` key; falling back to the
    system default in that case would be wrong (it would silently override the
    operator's explicit empty config). Use `is not None` semantics.
    """
    w = _watch(watched_item=_wi(default_schedule_config={}))
    assert resolved_schedule_config(w) == {}


def test_content_type_watch_overrides_watched_item():
    w = _watch(
        content_type=ContentType.PDF,
        watched_item=_wi(default_content_type=ContentType.HTML),
    )
    assert resolved_content_type(w) is ContentType.PDF


def test_content_type_falls_back_to_watched_item():
    w = _watch(content_type=None, watched_item=_wi(default_content_type=ContentType.HTML))
    assert resolved_content_type(w) is ContentType.HTML


def test_tags_merge_additively():
    w = _watch(tags=["b", "c"], watched_item=_wi(default_tags=["a", "b"]))
    assert resolved_tags(w) == ["a", "b", "c"]


def test_tags_empty_when_both_unset():
    w = _watch(tags=None, watched_item=_wi(default_tags=None))
    assert resolved_tags(w) == []


# ---------------------------------------------------------------------------
# resolved_notification_dispatches — Approach B union
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolved_notification_dispatches_unions_template_and_watch_configs(
    db_session,
):
    """Approach B: union of WatchedItem templates + Watch's own configs."""
    watch = await make_watch(db_session)

    template = WatchedItemNotificationTemplate(
        watched_item_id=watch.watched_item_id,
        channel_hint="slack",
        events=["change_detected"],
    )
    config = WatchNotificationConfig(
        watch_id=watch.id,
        channel_hint="email",
        events=["change_detected"],
    )
    db_session.add_all([template, config])
    await db_session.flush()

    rows = await resolved_notification_dispatches(db_session, watch)
    assert len(rows) == 2
    sources = {r.source for r in rows}
    assert sources == {"watched_item_template", "watch_config"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolved_notification_dispatches_templates_only(db_session):
    """Watch with no own configs, WatchedItem with 2 templates → 2 entries."""
    watch = await make_watch(db_session)

    t1 = WatchedItemNotificationTemplate(
        watched_item_id=watch.watched_item_id,
        channel_hint="slack",
        events=["change_detected"],
    )
    t2 = WatchedItemNotificationTemplate(
        watched_item_id=watch.watched_item_id,
        channel_hint="email",
        events=["change_detected"],
    )
    db_session.add_all([t1, t2])
    await db_session.flush()

    rows = await resolved_notification_dispatches(db_session, watch)
    assert len(rows) == 2
    assert all(r.source == "watched_item_template" for r in rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolved_notification_dispatches_own_config_only(db_session):
    """Watch with 1 own config, WatchedItem with 0 templates → 1 entry."""
    watch = await make_watch(db_session)

    config = WatchNotificationConfig(
        watch_id=watch.id,
        channel_hint="email",
        events=["change_detected"],
    )
    db_session.add(config)
    await db_session.flush()

    rows = await resolved_notification_dispatches(db_session, watch)
    assert len(rows) == 1
    assert rows[0].source == "watch_config"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolved_notification_dispatches_filters_inactive(db_session):
    """Inactive template and inactive config are excluded."""
    watch = await make_watch(db_session)

    inactive_template = WatchedItemNotificationTemplate(
        watched_item_id=watch.watched_item_id,
        channel_hint="slack",
        events=["change_detected"],
        is_active=False,
    )
    inactive_config = WatchNotificationConfig(
        watch_id=watch.id,
        channel_hint="email",
        events=["change_detected"],
        is_active=False,
    )
    db_session.add_all([inactive_template, inactive_config])
    await db_session.flush()

    rows = await resolved_notification_dispatches(db_session, watch)
    assert rows == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolved_notification_dispatches_filters_by_event_type(db_session):
    """Rows without the requested event_type are excluded."""
    watch = await make_watch(db_session)

    matching = WatchedItemNotificationTemplate(
        watched_item_id=watch.watched_item_id,
        channel_hint="slack",
        events=["change_detected"],
    )
    non_matching_template = WatchedItemNotificationTemplate(
        watched_item_id=watch.watched_item_id,
        channel_hint="slack",
        events=["watch_created"],
    )
    matching_config = WatchNotificationConfig(
        watch_id=watch.id,
        channel_hint="email",
        events=["change_detected"],
    )
    non_matching_config = WatchNotificationConfig(
        watch_id=watch.id,
        channel_hint="email",
        events=["watch_created"],
    )
    db_session.add_all([matching, non_matching_template, matching_config, non_matching_config])
    await db_session.flush()

    rows = await resolved_notification_dispatches(db_session, watch, event_type="change_detected")
    assert len(rows) == 2
    sources = {r.source for r in rows}
    assert sources == {"watched_item_template", "watch_config"}
