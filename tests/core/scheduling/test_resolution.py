"""Unit tests for 3-tier schedule resolution (#205).

resolved_schedule_config stays single-arg and reads two WatchedItem columns:
item default -> denormalized domain default -> system default. None means
"inherit"; a non-None config (including an explicit ``{}``) wins at its tier.
"""

from types import SimpleNamespace

from src.core.scheduling.resolution import SYSTEM_DEFAULT_SCHEDULE_CONFIG, resolved_schedule_config


def _wi(item=None, domain=None):
    """A minimal stand-in carrying just the two resolution columns."""
    return SimpleNamespace(
        default_schedule_config=item,
        domain_default_schedule_config=domain,
    )


def test_item_config_wins_over_domain_and_system():
    wi = _wi(item={"interval": "6h"}, domain={"interval": "7d"})
    assert resolved_schedule_config(wi) == {"interval": "6h"}


def test_domain_config_wins_when_item_unset():
    wi = _wi(item=None, domain={"interval": "7d"})
    assert resolved_schedule_config(wi) == {"interval": "7d"}


def test_system_default_when_item_and_domain_unset():
    wi = _wi(item=None, domain=None)
    assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG


def test_item_empty_config_passes_through_not_domain():
    """An explicit empty item config wins at its tier — does not fall to domain."""
    wi = _wi(item={}, domain={"interval": "7d"})
    assert resolved_schedule_config(wi) == {}


def test_domain_empty_config_passes_through_not_system():
    """An explicit empty domain config wins over system (write-boundary rejects {},
    but the resolver stays consistent with the item tier defensively)."""
    wi = _wi(item=None, domain={})
    assert resolved_schedule_config(wi) == {}
