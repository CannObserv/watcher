"""Tests for src.core.utils shared utilities."""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from src.core.utils import format_utc_iso, watched_item_event_base_metadata


class TestFormatUtcIso:
    def test_utc_aware_datetime(self):
        dt = datetime(2026, 4, 9, 14, 22, 37, tzinfo=UTC)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37Z"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2026, 4, 9, 14, 22, 37)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37Z"

    def test_non_utc_aware_datetime_coerced_to_utc(self):
        # UTC−5; 09:22:37−05:00 == 14:22:37Z
        eastern = timezone(timedelta(hours=-5))
        dt = datetime(2026, 4, 9, 9, 22, 37, tzinfo=eastern)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37Z"

    def test_microseconds_preserved(self):
        dt = datetime(2026, 4, 9, 14, 22, 37, 123456, tzinfo=UTC)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37.123456Z"

    def test_midnight(self):
        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert format_utc_iso(dt) == "2026-01-01T00:00:00Z"


def _wi(**overrides):
    """The attributes ``watched_item_event_base_metadata`` reads, plus the chain's."""
    base = {
        "domain_name": None,
        "default_schedule_config": None,
        "domain_default_schedule_config": None,
        "announced_schedule_config": None,
        "throttle_floor_interval": None,
        "last_changed_at": None,
        "default_tags": None,
        "description": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCheckIntervalReportsTheResolvedCadence:
    """#375: ``check_interval`` is the "Check cadence" template variable. It reads
    the resolved chain — announced, else the system default — not the item
    default, which no longer schedules anything."""

    def test_an_announced_interval_is_reported(self):
        meta = watched_item_event_base_metadata(_wi(announced_schedule_config={"interval": "15m"}))
        assert meta["check_interval"] == "15m"

    def test_an_item_default_is_not_reported_the_system_default_is(self):
        meta = watched_item_event_base_metadata(_wi(default_schedule_config={"interval": "6h"}))
        assert meta["check_interval"] == "1d"

    def test_an_intervalless_announcement_omits_the_key(self):
        meta = watched_item_event_base_metadata(_wi(announced_schedule_config={}))
        assert "check_interval" not in meta
