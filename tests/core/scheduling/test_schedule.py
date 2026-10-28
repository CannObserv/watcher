"""Unit tests for the definitive schedule-display helper (#206, #375).

``resolve_schedule_display`` is the single source of truth for the resolved
interval text, where it came from, and the next-check datetime — used by the
list view, the detail page, and the domain-detail table so display is
consistent with each other and with ``schedule_tick``.

Since F2 (#375) there are two sources, ``registry`` and ``default``: the item
and domain tiers, the throttle floor and the temporal-profile override no
longer move the cadence, so the display does not report them either.
"""

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.scheduling.schedule import ScheduleDisplay, resolve_schedule_display

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _wi(item=None, domain=None, last_checked_at=None, announced=None, floor=None):
    """A minimal WatchedItem stand-in carrying only the fields the helper reads."""
    return SimpleNamespace(
        default_schedule_config=item,
        domain_default_schedule_config=domain,
        announced_schedule_config=announced,
        throttle_floor_interval=floor,
        last_checked_at=last_checked_at,
    )


class TestSource:
    def test_announced_interval_is_source_registry(self):
        d = resolve_schedule_display(_wi(announced={"interval": "15m"}), now=NOW)
        assert d.interval_text == "15m"
        assert d.source == "registry"
        assert d.marker == "registry"

    def test_system_default_is_source_default(self):
        d = resolve_schedule_display(_wi(), now=NOW)
        assert d.interval_text == "1d"
        assert d.source == "default"
        assert d.marker == "default"

    def test_announced_empty_config_shows_braces(self):
        # An intervalless config shows the literal rather than a blank beside
        # the marker (#202 CR).
        d = resolve_schedule_display(_wi(announced={}), now=NOW)
        assert d.interval_text == "{ }"
        assert d.source == "registry"


class TestInertTiersAreNotReported:
    """#375: a value left in an inert column must not show as the cadence."""

    def test_an_item_interval_shows_the_system_default(self):
        d = resolve_schedule_display(_wi(item={"interval": "6h"}), now=NOW)
        assert d.interval_text == "1d"
        assert d.source == "default"
        assert d.marker == "default"

    def test_a_domain_interval_shows_the_system_default(self):
        d = resolve_schedule_display(_wi(domain={"interval": "7d"}), now=NOW)
        assert d.interval_text == "1d"
        assert d.marker == "default"

    def test_a_floor_does_not_show_or_mark(self):
        d = resolve_schedule_display(_wi(announced={"interval": "15m"}, floor="1d"), now=NOW)
        assert d.interval_text == "15m"
        assert d.marker == "registry"

    def test_the_display_carries_no_profile_or_throttle_fields(self):
        assert {f.name for f in fields(ScheduleDisplay)} == {
            "interval_text",
            "source",
            "next_check",
        }

    def test_profiles_are_no_longer_accepted(self):
        with pytest.raises(TypeError):
            resolve_schedule_display(_wi(), now=NOW, profiles=[])  # type: ignore[call-arg]


class TestNextCheck:
    def test_none_when_never_checked(self):
        d = resolve_schedule_display(_wi(last_checked_at=None), now=NOW)
        assert d.next_check is None

    def test_system_default_drives_next_check(self):
        last = NOW - timedelta(hours=3)
        d = resolve_schedule_display(_wi(last_checked_at=last), now=NOW)
        assert d.next_check == last + timedelta(days=1)

    def test_announced_interval_drives_next_check(self):
        last = NOW - timedelta(hours=3)
        d = resolve_schedule_display(
            _wi(announced={"interval": "6h"}, last_checked_at=last), now=NOW
        )
        assert d.next_check == last + timedelta(hours=6)

    def test_an_item_interval_does_not_drive_next_check(self):
        last = NOW - timedelta(hours=3)
        d = resolve_schedule_display(_wi(item={"interval": "1h"}, last_checked_at=last), now=NOW)
        assert d.next_check == last + timedelta(days=1)


def test_returns_frozen_dataclass():
    d = resolve_schedule_display(_wi(announced={"interval": "6h"}), now=NOW)
    assert isinstance(d, ScheduleDisplay)
    try:
        d.interval_text = "1d"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ScheduleDisplay should be frozen")
