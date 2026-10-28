"""Schedule resolution for a WatchedItem.

**Announced, else the system default (#375, F2).** Archiver resolves profile →
item → domain and announces a concrete ``interval`` on ``info.registry``
(CannObserv/archiver#364), so Watcher reads one column: the announced cadence,
or ``SYSTEM_DEFAULT_SCHEDULE_CONFIG`` when nothing is announced. The delegation
case — ``{"schema_version": 1}`` with no ``interval``, or an unparseable one —
leaves ``announced_schedule_config`` NULL and lands on the system default, the
only default Watcher keeps.

**Before F2 there were four tiers and a floor.** The chain was announced →
``WatchedItem.default_schedule_config`` → the Domain default (read from the
denormalized ``WatchedItem.domain_default_schedule_config``) → system default
(#205, #254), and the ``reduce_frequency`` post-action's
``throttle_floor_interval`` applied over it as a floor that could only slow an
item. The announced cadence had its own column so the hourly snapshot could not
overwrite the operator's item default or revert a throttle. Those three columns
and their writers are kept, inert, until the jump: the dashboard and API
surfaces go in #377/#378, the columns in #380, and
``src/ops/export_for_archiver.py`` still reads all of them for the pre-F3
re-export (#376).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation only. ``src.core.utils`` reads the resolved cadence for
    # ``check_interval`` and the notification renderer imports ``utils``, so a
    # runtime model import here would pull the store side into the renderer
    # (``tests/core/notifications/test_diff_layering.py``, #222 CR 2).
    from src.core.models.watched_item import WatchedItem

SYSTEM_DEFAULT_SCHEDULE_CONFIG: dict = {"interval": "1d"}


def resolved_schedule_config(watched_item: "WatchedItem") -> dict:
    """Resolve a WatchedItem's schedule config: announced, else the system default.

    ``None`` means "nothing announced" and falls to the default; a non-``None``
    config (including an explicit ``{}``) wins — ``{}`` passes through as "no
    interval" rather than falling to the default.

    **The result may alias a stored column** — the announced dict is returned by
    reference. Callers must treat it as read-only.
    """
    if watched_item.announced_schedule_config is not None:
        return watched_item.announced_schedule_config
    return SYSTEM_DEFAULT_SCHEDULE_CONFIG
