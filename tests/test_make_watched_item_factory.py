"""Tests for the test factory itself (#191: WatchedItem is the single entity)."""

import pytest

from src.core.models import WatchedItem
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration


async def test_make_watched_item_mints_the_info_item_link(db_session):
    """The InfoItem link defaults to a fresh ULID — nothing is created in Archiver's
    schema, which Watcher does not have (#271, #311)."""
    wi = await make_watched_item(db_session, name="Auto")
    assert isinstance(wi, WatchedItem)
    assert wi.archiver_info_item_id is not None
    assert wi.effective_url


async def test_make_watched_item_mints_a_distinct_info_item_per_call(db_session):
    """The InfoItem link is unique (one WatchedItem per InfoItem), so repeated
    calls must not collide."""
    first = await make_watched_item(db_session, name="First")
    second = await make_watched_item(db_session, name="Second")
    assert first.archiver_info_item_id != second.archiver_info_item_id


async def test_make_watched_item_links_the_info_source(db_session):
    """#251: both Archiver links are NOT NULL, so the factory always sets them."""
    wi = await make_watched_item(db_session, name="Linked")
    assert wi.archiver_info_source_id is not None
    assert wi.effective_url
