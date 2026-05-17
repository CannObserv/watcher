"""Tests for the resolution chain: Watch override → WatchedItem default → system default."""

from unittest.mock import MagicMock

from src.core.models.watch import ContentType
from src.core.watches.resolution import (
    SYSTEM_DEFAULT_SCHEDULE_CONFIG,
    resolved_content_type,
    resolved_schedule_config,
    resolved_tags,
)


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
