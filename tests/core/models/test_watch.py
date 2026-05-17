"""Round-trip tests for the Watch model's InfoItem-first reshape (#160)."""

from sqlalchemy import inspect

from src.core.models.watch import Watch


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
