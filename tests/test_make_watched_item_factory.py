"""Tests for the test factory itself (#191: WatchedItem is the single entity)."""

import pytest

from src.core.models import WatchedItem
from tests.conftest import make_watched_item

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
