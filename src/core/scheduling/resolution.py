"""Schedule resolution for a WatchedItem.

Three tiers (#205): WatchedItem default → Domain default → system default. The
Domain tier is read from ``WatchedItem.domain_default_schedule_config``, a
denormalized copy maintained on every create/PATCH path and back-filled on
domain-default edit (mirrors ``domain_suspended``), so the resolver stays
single-arg and the scheduler needs no live Domain join.
"""

from src.core.models.watched_item import WatchedItem

SYSTEM_DEFAULT_SCHEDULE_CONFIG: dict = {"interval": "1d"}


def resolved_schedule_config(watched_item: WatchedItem) -> dict:
    """Resolve a WatchedItem's schedule config across the 3-tier chain.

    Order: item ``default_schedule_config`` → denormalized
    ``domain_default_schedule_config`` → system default. At each tier `None`
    means "inherit" and falls through; a non-`None` config (including an explicit
    `{}`) wins at its tier — `{}` passes through as "no interval" rather than
    falling to the next tier, consistent across the item and domain tiers.
    """
    if watched_item.default_schedule_config is not None:
        return watched_item.default_schedule_config
    if watched_item.domain_default_schedule_config is not None:
        return watched_item.domain_default_schedule_config
    return SYSTEM_DEFAULT_SCHEDULE_CONFIG
