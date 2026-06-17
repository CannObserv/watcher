"""Tests for the test factory itself (#191: WatchedItem is the single entity)."""

import pytest

from src.core.models import WatchedItem
from tests.conftest import make_watch, make_watched_item

pytestmark = pytest.mark.integration


async def test_make_watched_item_auto_creates_info_item_and_source(db_session):
    """Factory wires up an InfoItem + primary InfoSource binding on the WatchedItem."""
    wi = await make_watched_item(db_session, name="Auto")
    assert isinstance(wi, WatchedItem)
    assert wi.archiver_info_item_id is not None
    assert wi.effective_url


async def test_make_watched_item_url_only(db_session):
    """auto_info_item=False yields a URL-only WatchedItem (no InfoItem)."""
    wi = await make_watched_item(db_session, name="URLOnly", auto_info_item=False)
    assert wi.archiver_info_item_id is None
    assert wi.effective_url


async def test_make_watch_shim_returns_watched_item(db_session):
    """The legacy make_watch shim returns a WatchedItem with self-ref aliases."""
    watch = await make_watch(db_session, name="Compat")
    assert isinstance(watch, WatchedItem)
    # Legacy aliases resolve to the WatchedItem itself.
    assert watch.watched_item is watch
    assert watch.watched_item_id == watch.id


async def test_make_watch_shim_maps_legacy_kwargs(db_session):
    """Legacy per-Watch kwargs map onto WatchedItem defaults."""
    watch = await make_watch(db_session, name="Mapped", content_type="pdf", tags=["a"])
    assert str(watch.default_content_type) == "pdf"
    assert watch.default_tags == ["a"]
