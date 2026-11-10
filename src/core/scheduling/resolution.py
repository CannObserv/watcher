"""Schedule resolution for a WatchedItem.

**Announced, else the system default (#375, F2).** Archiver resolves profile →
item → domain and announces a concrete ``interval`` on ``info.registry``
(CannObserv/archiver#364), so Watcher reads one column: the announced cadence,
or ``SYSTEM_DEFAULT_SCHEDULE_CONFIG`` when nothing is announced. The delegation
case — ``{"schema_version": 1}`` with no ``interval``, or an unparseable one —
leaves ``announced_schedule_config`` NULL and lands on the system default, the
only default Watcher keeps.

**Before F2 there were four tiers and a floor.** The chain was announced →
the item's own default → the Domain default (denormalized onto the item) →
system default (#205, #254), with the ``reduce_frequency`` post-action's
throttle floor applied over it. Those columns and their writers went with the
jump (#377, #378), the columns themselves in #380.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation only: this module needs no model at runtime. The layering
    # reason it once had (#222 CR 2) went with the notification renderer (#379).
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
