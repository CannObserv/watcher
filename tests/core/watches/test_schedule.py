"""Unit tests for the definitive schedule-display helper (#206).

``resolve_schedule_display`` is the single source of truth for the resolved
interval text, its inheritance source, whether a temporal profile is currently
overriding it, and the next-check datetime — used by the list view, the detail
page, and the domain-detail table so display is consistent with each other and
with ``schedule_tick``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.core.watches.schedule import ScheduleDisplay, resolve_schedule_display

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _wi(item=None, domain=None, last_checked_at=None):
    """A minimal WatchedItem stand-in carrying only the fields the helper reads."""
    return SimpleNamespace(
        default_schedule_config=item,
        domain_default_schedule_config=domain,
        last_checked_at=last_checked_at,
    )


class TestSource:
    """interval_text + source across the 3-tier chain (no profile)."""

    def test_explicit_item_interval_is_source_item(self):
        d = resolve_schedule_display(_wi(item={"interval": "6h"}), now=NOW)
        assert d.interval_text == "6h"
        assert d.source == "item"
        assert d.inherited is False

    def test_domain_inherited_is_source_domain(self):
        d = resolve_schedule_display(_wi(item=None, domain={"interval": "7d"}), now=NOW)
        assert d.interval_text == "7d"
        assert d.source == "domain"
        assert d.inherited is True

    def test_system_default_is_source_default(self):
        d = resolve_schedule_display(_wi(item=None, domain=None), now=NOW)
        assert d.interval_text == "1d"
        assert d.source == "default"
        assert d.inherited is True

    def test_empty_item_config_shows_braces_source_item(self):
        # A non-None intervalless item config wins at its tier; show the literal
        # rather than a blank beside an inherited tag (#202 CR).
        d = resolve_schedule_display(_wi(item={}, domain={"interval": "7d"}), now=NOW)
        assert d.interval_text == "{ }"
        assert d.source == "item"
        assert d.inherited is False


class TestNextCheck:
    def test_none_when_never_checked(self):
        d = resolve_schedule_display(_wi(item=None, last_checked_at=None), now=NOW)
        assert d.next_check is None

    def test_resolved_interval_drives_next_check(self):
        last = NOW - timedelta(hours=3)
        d = resolve_schedule_display(_wi(item=None, last_checked_at=last), now=NOW)
        # Inherited system default 1d → last + 1d.
        assert d.next_check == last + timedelta(days=1)

    def test_domain_interval_drives_next_check(self):
        last = NOW - timedelta(days=1)
        d = resolve_schedule_display(
            _wi(item=None, domain={"interval": "7d"}, last_checked_at=last), now=NOW
        )
        assert d.next_check == last + timedelta(days=7)


class TestProfileAware:
    """A currently-active profile overrides the base cadence in the display so the
    UI matches what ``schedule_tick`` actually does (#204 CR finding 2)."""

    def _event_profile(self, interval, days_before=30):
        return {
            "profile_type": "event",
            "reference_date": "2026-06-25",  # 5 days after NOW
            "rules": [{"days_before": days_before, "interval": interval}],
            "is_active": True,
        }

    def test_active_profile_overrides_interval_text(self):
        last = NOW - timedelta(minutes=30)  # not yet overdue at the 1h profile cadence
        d = resolve_schedule_display(
            _wi(item={"interval": "1d"}, last_checked_at=last),
            now=NOW,
            profiles=[self._event_profile("1h")],
        )
        assert d.profile_active is True
        assert d.interval_text == "1h"  # profile cadence, not the base 1d
        # next_check honors the profile too (1h cadence, not the base 1d).
        assert d.next_check == last + timedelta(hours=1)

    def test_inactive_window_profile_leaves_base_cadence(self):
        # reference_date already passed → profile no longer shortens anything.
        past_profile = {
            "profile_type": "event",
            "reference_date": "2026-06-19",  # yesterday relative to NOW
            "rules": [{"days_before": 30, "interval": "1h"}],
            "is_active": True,
        }
        d = resolve_schedule_display(_wi(item={"interval": "1d"}), now=NOW, profiles=[past_profile])
        assert d.profile_active is False
        assert d.interval_text == "1d"
        assert d.source == "item"

    def test_no_profiles_means_not_active(self):
        d = resolve_schedule_display(_wi(item={"interval": "1d"}), now=NOW)
        assert d.profile_active is False


def test_returns_frozen_dataclass():
    d = resolve_schedule_display(_wi(item={"interval": "6h"}), now=NOW)
    assert isinstance(d, ScheduleDisplay)
    try:
        d.interval_text = "1d"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ScheduleDisplay should be frozen")
