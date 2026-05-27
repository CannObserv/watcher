"""Round-trip tests for the Watch model's InfoItem-first reshape (#160)."""

from sqlalchemy import inspect

from src.core.models.watch import Watch
from src.core.models.watched_item import WatchedItem


def test_watch_new_shape_persists_info_item_and_target():
    """info_item_id required; target_info_source_id nullable; watched_item_id
    required; schedule_config absent.
    """
    cols = {c.name for c in inspect(Watch).columns}
    assert "info_item_id" in cols
    assert "target_info_source_id" in cols
    assert "watched_item_id" in cols
    assert "info_source_id" not in cols
    assert "schedule_config" not in cols


def test_watch_has_no_effective_domain_column():
    """effective_domain moved to WatchedItem.domain_name (#177)."""
    cols = {c.name for c in inspect(Watch).columns}
    assert "effective_domain" not in cols


def test_watched_item_has_domain_name_and_domain_suspended():
    """WatchedItem gains domain_name (FK → Domain.name) and domain_suspended (#177)."""
    cols = {c.name for c in inspect(WatchedItem).columns}
    assert "domain_name" in cols
    assert "domain_suspended" in cols


def test_watched_item_domain_name_defaults_none():
    """domain_name is nullable — standalone WatchedItems have no domain yet."""
    wi = WatchedItem(info_item_id=__import__("ulid").ULID(), name="Test")
    assert wi.domain_name is None


def test_watched_item_domain_suspended_defaults_false():
    """domain_suspended defaults False."""
    wi = WatchedItem(info_item_id=__import__("ulid").ULID(), name="Test")
    assert wi.domain_suspended is False
