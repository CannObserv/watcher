"""Tests for scheduler — compute next check time from schedule_config."""

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from src.core.scheduling import cadence
from src.core.scheduling.cadence import (
    compute_next_check,
    parse_interval,
    validate_optional_schedule_config,
)


class TestValidateOptionalScheduleConfig:
    """#205: write-boundary cadence validation shared by the domain API + dashboard."""

    def test_none_passes_through(self):
        assert validate_optional_schedule_config(None) is None

    def test_valid_interval_returned_unchanged(self):
        cfg = {"interval": "7d"}
        assert validate_optional_schedule_config(cfg) == cfg

    def test_empty_dict_rejected(self):
        with pytest.raises(ValueError, match="non-empty 'interval'"):
            validate_optional_schedule_config({})

    def test_blank_interval_rejected(self):
        with pytest.raises(ValueError, match="non-empty 'interval'"):
            validate_optional_schedule_config({"interval": ""})

    def test_malformed_interval_rejected(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            validate_optional_schedule_config({"interval": "soon"})

    @pytest.mark.parametrize("bad", [123, ["6h"], True, {"nested": 1}])
    def test_non_string_interval_raises_valueerror_not_typeerror(self, bad):
        """#205 CR: a non-string interval must raise ValueError (→ 422), not TypeError (→ 500)."""
        with pytest.raises(ValueError, match="must be a string"):
            validate_optional_schedule_config({"interval": bad})


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


class TestProfileEvaluationIsGone:
    """#375: Archiver evaluates temporal profiles and announces the result
    (CannObserv/archiver#364, #365), so Watcher no longer can."""

    @pytest.mark.parametrize(
        "name",
        [
            "resolve_effective_interval",
            "evaluate_post_actions",
            "_resolve_single_profile",
            "format_interval",
        ],
    )
    def test_the_profile_evaluator_is_removed(self, name):
        assert not hasattr(cadence, name)

    def test_compute_next_check_takes_no_profile_override(self):
        params = inspect.signature(compute_next_check).parameters
        assert "profiles" not in params
        assert "profile_interval" not in params
