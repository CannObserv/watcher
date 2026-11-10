"""Unit tests for schedule resolution (#205, #254, #375).

Since F2 (#375) ``resolved_schedule_config`` reads one WatchedItem column:
announced, else the system default. Archiver resolves profile → item → domain
and announces a concrete interval (CannObserv/archiver#364). The local item and
domain tiers and the throttle floor bound nothing after F2, and #380 dropped
their columns.

``None`` means "no announcement" and falls to the system default; a non-``None``
config (including an explicit ``{}``) wins, so ``{}`` passes through as "no
interval" rather than falling to the default.
"""

from types import SimpleNamespace

from src.core.scheduling.resolution import SYSTEM_DEFAULT_SCHEDULE_CONFIG, resolved_schedule_config


def _wi(announced=None):
    """A minimal stand-in carrying the one column resolution reads."""
    return SimpleNamespace(announced_schedule_config=announced)


class TestAnnouncedElseDefault:
    def test_announced_wins(self):
        wi = _wi(announced={"interval": "15m"})
        assert resolved_schedule_config(wi) == {"interval": "15m"}

    def test_system_default_when_nothing_is_announced(self):
        """Also the delegation case: ``{"schema_version": 1}`` with no
        ``interval`` leaves the announced column NULL."""
        assert resolved_schedule_config(_wi()) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_announced_empty_config_passes_through_not_default(self):
        """An explicit ``{}`` wins at its tier, as it did before F2."""
        assert resolved_schedule_config(_wi(announced={})) == {}

    def test_announced_does_not_mutate_the_stored_dict(self):
        announced = {"interval": "15m"}
        resolved_schedule_config(_wi(announced=announced))
        assert announced == {"interval": "15m"}
