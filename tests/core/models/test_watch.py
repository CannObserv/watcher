"""Watch model shape tests (#160 InfoItem-first, #185 Phase A Step 6 cleanup)."""

from sqlalchemy import inspect

from src.core.models.watch import Watch
from src.core.models.watched_item import WatchedItem


def test_watch_phase_a_shape():
    """Phase A: Watch is a thin content-target record.

    Dropped in step 6: info_item_id, target_info_source_id, effective_url,
    last_checked_at, last_changed_at, health_status.
    Renamed: domain_suspended → suspended_by_domain.
    Retained: id, watched_item_id, name, content_type, is_active, is_archived,
    suspended_by_domain, tags, description, created_at, updated_at.
    """
    cols = {c.name for c in inspect(Watch).columns}
    # Present.
    assert "id" in cols
    assert "watched_item_id" in cols
    assert "name" in cols
    assert "suspended_by_domain" in cols
    # Removed.
    assert "info_item_id" not in cols
    assert "target_info_source_id" not in cols
    assert "effective_url" not in cols
    assert "last_checked_at" not in cols
    assert "last_changed_at" not in cols
    assert "health_status" not in cols
    assert "domain_suspended" not in cols
    # Pre-existing absent columns.
    assert "info_source_id" not in cols
    assert "schedule_config" not in cols
    assert "effective_domain" not in cols


def test_watch_suspended_by_domain_defaults_false():
    """suspended_by_domain defaults False."""
    from ulid import ULID

    w = Watch(name="Test", watched_item_id=ULID())
    assert w.suspended_by_domain is False


def test_watched_item_has_domain_name_and_domain_suspended():
    """WatchedItem retains domain_name and domain_suspended (#177)."""
    cols = {c.name for c in inspect(WatchedItem).columns}
    assert "domain_name" in cols
    assert "domain_suspended" in cols


def test_watched_item_has_health_and_timestamps():
    """WatchedItem owns last_checked_at, last_changed_at, health_status (#185 step 6)."""
    cols = {c.name for c in inspect(WatchedItem).columns}
    assert "last_checked_at" in cols
    assert "last_changed_at" in cols
    assert "health_status" in cols


def test_watched_item_domain_name_defaults_none():
    """domain_name is nullable — standalone WatchedItems have no domain yet."""
    wi = WatchedItem(info_item_id=__import__("ulid").ULID(), name="Test")
    assert wi.domain_name is None


def test_watched_item_domain_suspended_defaults_false():
    """domain_suspended defaults False."""
    wi = WatchedItem(info_item_id=__import__("ulid").ULID(), name="Test")
    assert wi.domain_suspended is False
