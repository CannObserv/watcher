"""Tests for src.core.utils shared utilities."""

from datetime import UTC, datetime

from src.core.utils import format_utc_iso


class TestFormatUtcIso:
    def test_utc_aware_datetime(self):
        dt = datetime(2026, 4, 9, 14, 22, 37, tzinfo=UTC)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37Z"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2026, 4, 9, 14, 22, 37)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37Z"

    def test_non_utc_aware_datetime_coerced_to_utc(self):
        eastern = UTC  # use a fixed offset for determinism
        dt = datetime(2026, 4, 9, 14, 22, 37, tzinfo=eastern)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37Z"

    def test_microseconds_preserved(self):
        dt = datetime(2026, 4, 9, 14, 22, 37, 123456, tzinfo=UTC)
        assert format_utc_iso(dt) == "2026-04-09T14:22:37.123456Z"

    def test_midnight(self):
        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert format_utc_iso(dt) == "2026-01-01T00:00:00Z"
