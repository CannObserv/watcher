"""Tests for the notification cutover instant T (#376 part 1, F3).

At F3 Watcher dispatches only the events that occurred before T, and Archiver
those at or after it. Both services must parse the same string to the same
instant, so the grammar is Archiver's own; a value either one would misread is
a double send or a silent gap, so it refuses startup.
"""

import logging
import os
from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.core.notify_cutover import (
    NOTIFY_CUTOVER_AT_ENV,
    NotifyCutoverInvalid,
    assert_notify_cutover_at,
    format_cutover,
    notify_cutover_at,
)


class TestNotifyCutoverAt:
    def test_unset_means_no_cutover(self) -> None:
        assert notify_cutover_at({}) is None

    def test_blank_means_no_cutover(self) -> None:
        """As on Archiver: an empty assignment in an env file is not a T."""
        assert notify_cutover_at({NOTIFY_CUTOVER_AT_ENV: "   "}) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-11-06T21:00:00Z", datetime(2026, 11, 6, 21, tzinfo=UTC)),
            ("2026-11-06T21:00:00+00:00", datetime(2026, 11, 6, 21, tzinfo=UTC)),
            (
                "2026-11-06T21:00:00.123456Z",
                datetime(2026, 11, 6, 21, 0, 0, 123456, tzinfo=UTC),
            ),
            ("2026-11-06T21:00:00.5Z", datetime(2026, 11, 6, 21, 0, 0, 500000, tzinfo=UTC)),
            (" 2026-11-06T21:00:00Z ", datetime(2026, 11, 6, 21, tzinfo=UTC)),
        ],
    )
    def test_a_utc_instant_parses(self, value: str, expected: datetime) -> None:
        parsed = notify_cutover_at({NOTIFY_CUTOVER_AT_ENV: value})
        assert parsed == expected
        assert parsed.utcoffset().total_seconds() == 0

    @pytest.mark.parametrize(
        "value",
        [
            "2026-11-06",  # a date alone: whose midnight?
            "2026-11-06T21:00:00",  # naive
            "2026-11-06T21:00Z",  # no seconds: Archiver's grammar refuses it
            "2026-11-06T13:00:00-08:00",  # the same instant, but not spelled UTC
            "2026-11-06T21:00:00-00:00",
            "2026-11-06T21:00:00+01:00",
            "2026-11-06 21:00:00Z",  # a space, not the T
            "2026-11-06T21:00:00.1234567Z",  # seven fraction digits
            "2026-13-06T21:00:00Z",  # matches the shape, is no date
            "2026-11-06T25:00:00Z",
            "tomorrow",
        ],
    )
    def test_anything_else_is_refused(self, value: str) -> None:
        """Every one refused as this type, so the lifespan's except clause sees it."""
        with pytest.raises(NotifyCutoverInvalid):
            notify_cutover_at({NOTIFY_CUTOVER_AT_ENV: value})

    def test_the_refusal_names_the_variable_and_the_value(self) -> None:
        with pytest.raises(NotifyCutoverInvalid) as refused:
            notify_cutover_at({NOTIFY_CUTOVER_AT_ENV: "2026-11-06T21:00:00"})
        assert NOTIFY_CUTOVER_AT_ENV in str(refused.value)
        assert "2026-11-06T21:00:00" in str(refused.value)


class TestAssertNotifyCutoverAt:
    """The startup check, and the journal line flip step 3 reads T back from."""

    def test_malformed_refuses(self) -> None:
        with pytest.raises(NotifyCutoverInvalid):
            assert_notify_cutover_at({NOTIFY_CUTOVER_AT_ENV: "2026-11-06"})

    def test_unset_says_so(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="src.core.notify_cutover"):
            assert_notify_cutover_at({})
        assert [r.getMessage() for r in caplog.records] == [
            "notify cutover: unset (Watcher dispatches every event)"
        ]

    def test_set_prints_t_in_the_form_archiver_prints_it(self, caplog) -> None:
        """``YYYY-MM-DDTHH:MM:SS.ffffffZ``, whichever UTC spelling was set, so the
        two journals can be compared byte for byte before T arrives."""
        with caplog.at_level(logging.INFO, logger="src.core.notify_cutover"):
            assert_notify_cutover_at({NOTIFY_CUTOVER_AT_ENV: "2026-11-06T21:00:00+00:00"})
        assert [r.getMessage() for r in caplog.records] == [
            "notify cutover: 2026-11-06T21:00:00.000000Z (events at or after it are Archiver's)"
        ]


class TestFormatCutover:
    def test_six_fraction_digits_always(self) -> None:
        assert (
            format_cutover(datetime(2026, 11, 6, 21, tzinfo=UTC)) == "2026-11-06T21:00:00.000000Z"
        )

    def test_a_non_utc_aware_instant_is_shown_in_utc(self) -> None:
        """The suppression line formats ``occurred_at`` too; a ``Z`` on a local
        wall time would misstate the instant the drain check reads."""
        pst = timezone(timedelta(hours=-8))
        assert (
            format_cutover(datetime(2026, 11, 6, 13, tzinfo=pst)) == "2026-11-06T21:00:00.000000Z"
        )


def test_the_session_starts_without_a_cutover() -> None:
    """``tests/conftest.py`` pops it: ``/etc/watcher/.env`` holds T from F3 on, and
    a suite inheriting it would suppress every event built on ``now()`` — the
    result would depend on which shell ran it. Tests set their own T."""
    assert NOTIFY_CUTOVER_AT_ENV not in os.environ
