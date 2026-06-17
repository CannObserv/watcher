"""Schedule resolution for a WatchedItem.

Post-#191 the WatchedItem is the single monitored entity; there is no per-Watch
override layer. Schedule resolution reduces to: WatchedItem default → system
default. Kept as a function so the system default lives in one place.
"""

from src.core.models.watched_item import WatchedItem

SYSTEM_DEFAULT_SCHEDULE_CONFIG: dict = {"interval": "1d"}


def resolved_schedule_config(watched_item: WatchedItem) -> dict:
    """Resolve a WatchedItem's schedule config, falling back to the system default.

    Distinguishes `None` (no config set) from `{}` (explicitly empty config).
    Empty dict passes through; `None` falls back to the system default.
    """
    if watched_item.default_schedule_config is not None:
        return watched_item.default_schedule_config
    return SYSTEM_DEFAULT_SCHEDULE_CONFIG
