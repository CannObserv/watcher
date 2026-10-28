"""Unit tests for schedule resolution (#205, #254, #375).

Since F2 (#375) ``resolved_schedule_config`` reads one WatchedItem column:
announced, else the system default. Archiver resolves profile → item → domain
and announces a concrete interval (CannObserv/archiver#364), so the local item
and domain tiers and the throttle floor no longer move anything. Their columns
are kept until the jump's migrations (#380) — and these tests pin that a value
left in them is ignored.

``None`` means "no announcement" and falls to the system default; a non-``None``
config (including an explicit ``{}``) wins, so ``{}`` passes through as "no
interval" rather than falling to the default.
"""

from types import SimpleNamespace

from src.core.scheduling.resolution import SYSTEM_DEFAULT_SCHEDULE_CONFIG, resolved_schedule_config


def _wi(item=None, domain=None, announced=None, floor=None):
    """A minimal stand-in carrying the announced column and the inert ones."""
    return SimpleNamespace(
        default_schedule_config=item,
        domain_default_schedule_config=domain,
        announced_schedule_config=announced,
        throttle_floor_interval=floor,
    )


class TestAnnouncedElseDefault:
    def test_announced_wins(self):
        wi = _wi(announced={"interval": "15m"})
        assert resolved_schedule_config(wi) == {"interval": "15m"}

    def test_system_default_when_nothing_is_announced(self):
        assert resolved_schedule_config(_wi()) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_announced_empty_config_passes_through_not_default(self):
        """An explicit ``{}`` wins at its tier, as it did before F2."""
        assert resolved_schedule_config(_wi(announced={})) == {}

    def test_announced_does_not_mutate_the_stored_dict(self):
        announced = {"interval": "15m"}
        resolved_schedule_config(_wi(announced=announced, floor="1d"))
        assert announced == {"interval": "15m"}


class TestLocalTiersAreInert:
    """#375: the item and domain defaults no longer resolve anything."""

    def test_an_item_default_without_an_announcement_resolves_to_the_system_default(self):
        wi = _wi(item={"interval": "6h"})
        assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_a_domain_default_without_an_announcement_resolves_to_the_system_default(self):
        wi = _wi(domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_the_delegation_case_lands_on_the_system_default(self):
        """``{"schema_version": 1}`` with no ``interval`` leaves the announced
        column NULL. Before F2 that fell to the per-domain default; now it is
        the system default, the only default Watcher keeps."""
        wi = _wi(announced=None, item={"interval": "6h"}, domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_an_announcement_still_beats_both(self):
        wi = _wi(announced={"interval": "15m"}, item={"interval": "6h"}, domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == {"interval": "15m"}


class TestThrottleFloorIsInert:
    """#375: the floor was the ``reduce_frequency`` post-action's output; with the
    post-action gone, a value left in the column no longer slows anything."""

    def test_a_set_floor_does_not_slow_a_faster_announced_interval(self):
        wi = _wi(announced={"interval": "15m"}, floor="1d")
        assert resolved_schedule_config(wi) == {"interval": "15m"}

    def test_a_set_floor_does_not_slow_the_system_default(self):
        wi = _wi(floor="7d")
        assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG
