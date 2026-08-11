"""Unit tests for schedule resolution (#205, #254).

``resolved_schedule_config`` stays single-arg and reads three WatchedItem
columns: announced -> item default -> denormalized domain default -> system
default. ``None`` means "inherit"; a non-``None`` config (including an explicit
``{}``) wins at its tier.

The announced tier is #254: the registry owns cadence policy, so a parsed
``watch_spec`` interval outranks the local layers. It is a *separate column*
from ``default_schedule_config`` deliberately — that one has two other writers
(the operator and the ``reduce_frequency`` post-action), and letting the hourly
snapshot reconcile into it would silently revert every throttle.

The throttle floor is the other half of that split: ``reduce_frequency`` is
protective *mechanism*, not policy, so it applies as a floor over whatever the
chain resolves rather than as a tier inside it. A floor only ever slows an item
down; it can never speed one up past the announced cadence.
"""

from types import SimpleNamespace

import pytest

from src.core.scheduling.resolution import SYSTEM_DEFAULT_SCHEDULE_CONFIG, resolved_schedule_config


def _wi(item=None, domain=None, announced=None, floor=None):
    """A minimal stand-in carrying just the resolution columns."""
    return SimpleNamespace(
        default_schedule_config=item,
        domain_default_schedule_config=domain,
        announced_schedule_config=announced,
        throttle_floor_interval=floor,
    )


class TestBaseChain:
    def test_item_config_wins_over_domain_and_system(self):
        wi = _wi(item={"interval": "6h"}, domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == {"interval": "6h"}

    def test_domain_config_wins_when_item_unset(self):
        wi = _wi(item=None, domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == {"interval": "7d"}

    def test_system_default_when_item_and_domain_unset(self):
        wi = _wi(item=None, domain=None)
        assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_item_empty_config_passes_through_not_domain(self):
        """An explicit empty item config wins at its tier — does not fall to domain."""
        wi = _wi(item={}, domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == {}

    def test_domain_empty_config_passes_through_not_system(self):
        """An explicit empty domain config wins over system (write-boundary rejects {},
        but the resolver stays consistent with the item tier defensively)."""
        wi = _wi(item=None, domain={})
        assert resolved_schedule_config(wi) == {}


class TestAnnouncedTier:
    """#254: the registry owns cadence policy and outranks every local tier."""

    def test_announced_wins_over_item_domain_and_system(self):
        wi = _wi(announced={"interval": "15m"}, item={"interval": "6h"}, domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == {"interval": "15m"}

    def test_null_announced_falls_to_the_local_chain(self):
        """The contract's delegation case.

        ``{"schema_version": 1}`` with no ``interval`` means *apply your own
        default*, and the reconcile spells that by leaving this column NULL —
        which lands on the per-domain default, the layer cannobserv#324
        deliberately kept live.
        """
        wi = _wi(announced=None, item=None, domain={"interval": "7d"})
        assert resolved_schedule_config(wi) == {"interval": "7d"}

    def test_announced_does_not_mutate_the_stored_dict(self):
        announced = {"interval": "15m"}
        wi = _wi(announced=announced, floor="1d")
        resolved_schedule_config(wi)
        assert announced == {"interval": "15m"}


class TestThrottleFloor:
    """``reduce_frequency`` is mechanism: it slows, never speeds, and it survives
    an announcement because it does not live in a tier the reconcile writes."""

    def test_floor_slows_a_faster_resolved_interval(self):
        wi = _wi(announced={"interval": "15m"}, floor="1d")
        assert resolved_schedule_config(wi) == {"interval": "1d"}

    def test_floor_never_speeds_up_a_slower_interval(self):
        """A 7d domain cadence under a 1d floor stays 7d — pinning it to the floor
        would *increase* frequency, the opposite of what a throttle is for."""
        wi = _wi(domain={"interval": "7d"}, floor="1d")
        assert resolved_schedule_config(wi) == {"interval": "7d"}

    def test_floor_preserves_the_rest_of_the_config(self):
        wi = _wi(announced={"interval": "15m", "jitter": "5m"}, floor="1d")
        assert resolved_schedule_config(wi) == {"interval": "1d", "jitter": "5m"}

    def test_no_floor_is_a_no_op(self):
        wi = _wi(announced={"interval": "15m"}, floor=None)
        assert resolved_schedule_config(wi) == {"interval": "15m"}

    def test_unparseable_resolved_interval_takes_the_floor(self):
        """Defensive: the reconcile validates before storing, so this should be
        unreachable — but the read path is the scheduler's hot loop and must not
        raise. The floor is the safe direction."""
        wi = _wi(item={"interval": "not-an-interval"}, floor="1d")
        assert resolved_schedule_config(wi) == {"interval": "1d"}

    @pytest.mark.parametrize("floor", ["", None])
    def test_empty_floor_is_absent(self, floor):
        wi = _wi(announced={"interval": "15m"}, floor=floor)
        assert resolved_schedule_config(wi) == {"interval": "15m"}
