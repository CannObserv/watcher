"""Unit tests for the TemporalProfile model helpers."""

from datetime import date

from ulid import ULID

from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile


def test_to_resolution_dict_exposes_the_resolver_key_set():
    """#206 CR-5: to_resolution_dict is the canonical input shape shared by
    schedule_tick and the dashboard — lock the exact keys the scheduler resolvers
    (resolve_effective_interval / evaluate_post_actions) read, plus the id."""
    wi_id = ULID()
    profile = TemporalProfile(
        watched_item_id=wi_id,
        profile_type=ProfileType.EVENT,
        reference_date=date(2026, 7, 1),
        rules=[{"days_before": 7, "interval": "1h"}],
        post_action=PostAction.REDUCE_FREQUENCY,
    )
    profile.id = ULID()

    d = profile.to_resolution_dict()

    assert set(d) == {
        "id",
        "profile_type",
        "reference_date",
        "date_range_start",
        "date_range_end",
        "rules",
        "post_action",
        "is_active",
    }
    assert d["id"] == str(profile.id)  # serialized to str for JSON-safe transport
    assert d["profile_type"] == ProfileType.EVENT
    assert d["reference_date"] == date(2026, 7, 1)
    assert d["rules"] == [{"days_before": 7, "interval": "1h"}]
    assert d["post_action"] == PostAction.REDUCE_FREQUENCY
    assert d["is_active"] is True  # __init__ default


def test_to_resolution_dict_carries_seasonal_range_and_inactive_flag():
    profile = TemporalProfile(
        watched_item_id=ULID(),
        profile_type=ProfileType.SEASONAL,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 3, 31),
        rules=[{"interval": "6h"}],
        post_action=PostAction.DEACTIVATE,
        is_active=False,
    )
    profile.id = ULID()

    d = profile.to_resolution_dict()

    assert d["date_range_start"] == date(2026, 1, 1)
    assert d["date_range_end"] == date(2026, 3, 31)
    assert d["reference_date"] is None
    assert d["is_active"] is False
