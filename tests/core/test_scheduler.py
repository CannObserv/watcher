"""Tests for scheduler — compute next check time from schedule_config."""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.scheduler import compute_next_check, parse_interval


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
