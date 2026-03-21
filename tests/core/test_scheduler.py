"""Tests for scheduler — compute next check time from schedule_config."""

from datetime import UTC, date, datetime, timedelta

import pytest

from src.core.scheduler import (
    compute_next_check,
    evaluate_post_actions,
    parse_interval,
    resolve_effective_interval,
)


class TestParseInterval:
    def test_parse_seconds(self):
        assert parse_interval("30s") == timedelta(seconds=30)

    def test_parse_minutes(self):
        assert parse_interval("15m") == timedelta(minutes=15)

    def test_parse_hours(self):
        assert parse_interval("6h") == timedelta(hours=6)

    def test_parse_days(self):
        assert parse_interval("1d") == timedelta(days=1)

    def test_default_is_daily(self):
        assert parse_interval(None) == timedelta(days=1)

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("abc")


class TestComputeNextCheck:
    def test_no_previous_check_returns_now(self):
        now = datetime.now(UTC)
        cfg = {"interval": "1h"}
        result = compute_next_check(schedule_config=cfg, last_checked_at=None, now=now)
        assert result <= now

    def test_interval_from_last_check(self):
        now = datetime.now(UTC)
        last = now - timedelta(minutes=30)
        cfg = {"interval": "1h"}
        result = compute_next_check(schedule_config=cfg, last_checked_at=last, now=now)
        expected = last + timedelta(hours=1)
        assert result == expected

    def test_overdue_returns_now(self):
        now = datetime.now(UTC)
        last = now - timedelta(hours=2)
        cfg = {"interval": "1h"}
        result = compute_next_check(schedule_config=cfg, last_checked_at=last, now=now)
        assert result <= now

    def test_default_interval_daily(self):
        now = datetime.now(UTC)
        last = now - timedelta(hours=12)
        result = compute_next_check(schedule_config={}, last_checked_at=last, now=now)
        expected = last + timedelta(days=1)
        assert result == expected


class TestResolveEffectiveInterval:
    def test_no_profiles_returns_none(self):
        assert resolve_effective_interval([]) is None

    def test_event_profile_30_days_before(self):
        """25 days before event matches '30 days before' rule -> 6h."""
        today = date(2026, 3, 21)
        profile = {
            "type": "event",
            "is_active": True,
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 30, "interval": "6h"},
                {"days_before": 7, "interval": "1h"},
            ],
        }
        result = resolve_effective_interval([profile], today=today)
        assert result == timedelta(hours=6)

    def test_event_profile_7_days_before(self):
        """5 days before event matches '7 days before' rule -> 1h."""
        today = date(2026, 4, 10)
        profile = {
            "type": "event",
            "is_active": True,
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 30, "interval": "6h"},
                {"days_before": 7, "interval": "1h"},
            ],
        }
        result = resolve_effective_interval([profile], today=today)
        assert result == timedelta(hours=1)

    def test_event_profile_after_event_returns_none(self):
        today = date(2026, 4, 16)
        profile = {
            "type": "event",
            "is_active": True,
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 30, "interval": "6h"},
            ],
        }
        assert resolve_effective_interval([profile], today=today) is None

    def test_seasonal_within_range(self):
        today = date(2026, 6, 15)
        profile = {
            "type": "seasonal",
            "is_active": True,
            "date_range_start": "2026-06-01",
            "date_range_end": "2026-08-31",
            "rules": [{"interval": "1h"}],
        }
        result = resolve_effective_interval([profile], today=today)
        assert result == timedelta(hours=1)

    def test_seasonal_outside_range_returns_none(self):
        today = date(2026, 5, 15)
        profile = {
            "type": "seasonal",
            "is_active": True,
            "date_range_start": "2026-06-01",
            "date_range_end": "2026-08-31",
            "rules": [{"interval": "1h"}],
        }
        assert resolve_effective_interval([profile], today=today) is None

    def test_deadline_approaching(self):
        """10 days before deadline matches '14 days before' rule -> 12h."""
        today = date(2026, 4, 5)
        profile = {
            "type": "deadline",
            "is_active": True,
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 14, "interval": "12h"},
                {"days_before": 3, "interval": "1h"},
            ],
        }
        result = resolve_effective_interval([profile], today=today)
        assert result == timedelta(hours=12)

    def test_deadline_after_date_returns_none(self):
        today = date(2026, 4, 16)
        profile = {
            "type": "deadline",
            "is_active": True,
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 14, "interval": "12h"},
            ],
        }
        assert resolve_effective_interval([profile], today=today) is None

    def test_multiple_profiles_picks_shortest(self):
        """event 6h + deadline 2h -> 2h."""
        today = date(2026, 4, 1)
        event_profile = {
            "type": "event",
            "is_active": True,
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "6h"}],
        }
        deadline_profile = {
            "type": "deadline",
            "is_active": True,
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "2h"}],
        }
        result = resolve_effective_interval([event_profile, deadline_profile], today=today)
        assert result == timedelta(hours=2)

    def test_inactive_profile_ignored(self):
        today = date(2026, 4, 1)
        profile = {
            "type": "event",
            "is_active": False,
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "6h"}],
        }
        assert resolve_effective_interval([profile], today=today) is None


class TestComputeNextCheckWithProfiles:
    def test_profile_overrides_base_interval(self):
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        last = now - timedelta(hours=3)
        cfg = {"interval": "1d"}
        profiles = [
            {
                "type": "event",
                "is_active": True,
                "reference_date": "2026-04-15",
                "rules": [{"days_before": 30, "interval": "6h"}],
            }
        ]
        result = compute_next_check(cfg, last, now=now, profiles=profiles)
        expected = last + timedelta(hours=6)
        assert result == expected

    def test_no_profile_match_uses_base(self):
        now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        last = now - timedelta(hours=12)
        cfg = {"interval": "1d"}
        profiles = [
            {
                "type": "event",
                "is_active": True,
                "reference_date": "2026-04-15",
                "rules": [{"days_before": 30, "interval": "6h"}],
            }
        ]
        result = compute_next_check(cfg, last, now=now, profiles=profiles)
        expected = last + timedelta(days=1)
        assert result == expected


class TestEvaluatePostActions:
    def test_no_profiles_no_actions(self):
        assert evaluate_post_actions([]) == []

    def test_event_past_returns_action(self):
        today = date(2026, 4, 16)
        profile = {
            "type": "event",
            "is_active": True,
            "reference_date": "2026-04-15",
            "post_action": "deactivate",
            "rules": [{"days_before": 30, "interval": "6h"}],
        }
        result = evaluate_post_actions([profile], today=today)
        assert len(result) == 1
        assert result[0]["action"] == "deactivate"
        assert result[0]["profile"] is profile

    def test_event_not_yet_passed_no_action(self):
        today = date(2026, 4, 10)
        profile = {
            "type": "event",
            "is_active": True,
            "reference_date": "2026-04-15",
            "post_action": "deactivate",
            "rules": [{"days_before": 30, "interval": "6h"}],
        }
        assert evaluate_post_actions([profile], today=today) == []

    def test_seasonal_past_end_returns_action(self):
        today = date(2026, 9, 1)
        profile = {
            "type": "seasonal",
            "is_active": True,
            "date_range_start": "2026-06-01",
            "date_range_end": "2026-08-31",
            "post_action": "reduce_frequency",
            "rules": [{"interval": "1h"}],
        }
        result = evaluate_post_actions([profile], today=today)
        assert len(result) == 1
        assert result[0]["action"] == "reduce_frequency"
        assert result[0]["profile"] is profile

    def test_inactive_profile_no_action(self):
        today = date(2026, 4, 16)
        profile = {
            "type": "event",
            "is_active": False,
            "reference_date": "2026-04-15",
            "post_action": "deactivate",
            "rules": [{"days_before": 30, "interval": "6h"}],
        }
        assert evaluate_post_actions([profile], today=today) == []
