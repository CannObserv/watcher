"""Tests for the persistent-error re-notify knob and gate (#71)."""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from src.core.notifications.renotify import (
    DEFAULT_ERROR_RENOTIFY_INTERVAL,
    ERROR_RENOTIFY_INTERVAL_ENV,
    error_renotify_due,
    error_renotify_interval,
    error_renotify_metadata,
)
from src.core.utils import format_utc_iso

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


class TestErrorRenotifyInterval:
    def test_unset_is_a_day(self, monkeypatch):
        monkeypatch.delenv(ERROR_RENOTIFY_INTERVAL_ENV, raising=False)
        assert error_renotify_interval() == timedelta(hours=24)
        assert DEFAULT_ERROR_RENOTIFY_INTERVAL == "24h"

    def test_reads_the_interval_vocabulary(self, monkeypatch):
        monkeypatch.setenv(ERROR_RENOTIFY_INTERVAL_ENV, "6h")
        assert error_renotify_interval() == timedelta(hours=6)

    def test_an_unparseable_value_falls_back_loudly(self, monkeypatch, caplog):
        """A knob read on the failure path must not be able to wedge it."""
        monkeypatch.setenv(ERROR_RENOTIFY_INTERVAL_ENV, "daily")
        with caplog.at_level(logging.WARNING):
            assert error_renotify_interval() == timedelta(hours=24)
        assert ERROR_RENOTIFY_INTERVAL_ENV in caplog.text

    def test_zero_is_honoured_but_said_out_loud(self, monkeypatch, caplog):
        monkeypatch.setenv(ERROR_RENOTIFY_INTERVAL_ENV, "0s")
        with caplog.at_level(logging.INFO):
            assert error_renotify_interval() == timedelta(0)
        assert "every failed check" in caplog.text


class TestErrorRenotifyDue:
    @pytest.mark.parametrize(
        ("last", "due"),
        [
            (None, True),  # ERROR with no record of telling anyone
            (NOW - timedelta(hours=1), False),
            (NOW - timedelta(hours=24), True),  # the boundary is due
            (NOW - timedelta(hours=25), True),
        ],
    )
    def test_gate(self, last, due):
        assert error_renotify_due(last, now=NOW, interval=timedelta(hours=24)) is due


class TestErrorRenotifyMetadata:
    def test_first_notification_says_it_is_not_a_repeat(self):
        assert error_renotify_metadata(repeat=False, previously_notified_at=None) == {
            "renotify": False,
            "previously_notified_at": "",
        }

    def test_repeat_names_when_it_last_told_anyone(self):
        told = NOW - timedelta(days=1)
        assert error_renotify_metadata(repeat=True, previously_notified_at=told) == {
            "renotify": True,
            "previously_notified_at": format_utc_iso(told),
        }

    def test_repeat_without_a_record_leaves_the_time_empty_rather_than_invent_one(self):
        assert error_renotify_metadata(repeat=True, previously_notified_at=None) == {
            "renotify": True,
            "previously_notified_at": "",
        }

    def test_a_first_alert_never_carries_a_previous_time(self):
        """The stamp of a closed episode is cleared on recovery, but a first
        alert must say "empty" whatever it is handed."""
        told = NOW - timedelta(days=3)
        meta = error_renotify_metadata(repeat=False, previously_notified_at=told)
        assert meta["previously_notified_at"] == ""
