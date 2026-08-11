"""Schedule resolution for a WatchedItem.

Four tiers (#205, #254): announced → WatchedItem default → Domain default →
system default. The Domain tier is read from
``WatchedItem.domain_default_schedule_config``, a denormalized copy maintained on
every create/PATCH path and back-filled on domain-default edit (mirrors
``domain_suspended``), so the resolver stays single-arg and the scheduler needs
no live Domain join.

**Why the announced cadence gets its own column.** ``default_schedule_config``
already has two writers — the operator, and the ``reduce_frequency`` post-action
in ``schedule_tick`` — so reconciling the registry's ``watch_spec`` into it would
let an hourly snapshot silently revert every throttle, and would contaminate the
column CannObserv/archiver#150 imports *out of* Watcher. A separate column also
gives the contract's delegation case a natural spelling: ``{"schema_version": 1}``
with no ``interval`` means *apply your own default*, and the reconcile says that
by leaving ``announced_schedule_config`` NULL — which lands on the per-domain
tier, the layer cannobserv#324 deliberately kept live.

**The throttle is a floor, not a tier.** ``reduce_frequency`` is protective
mechanism rather than cadence policy — the same category as backoff, which
CannObserv/archiver#150's break-glass ruling explicitly left on Watcher's side of
the line. A floor composes with the announced interval instead of competing with
it: it can only ever slow an item down, never speed one past what the registry
asked for.
"""

from src.core.models.watched_item import WatchedItem
from src.core.scheduling.cadence import parse_interval

SYSTEM_DEFAULT_SCHEDULE_CONFIG: dict = {"interval": "1d"}


def base_schedule_config(watched_item: WatchedItem) -> dict:
    """The 4-tier chain, before the throttle floor.

    At each tier ``None`` means "inherit" and falls through; a non-``None``
    config (including an explicit ``{}``) wins at its tier — ``{}`` passes
    through as "no interval" rather than falling to the next tier, consistent
    across all four.
    """
    if watched_item.announced_schedule_config is not None:
        return watched_item.announced_schedule_config
    if watched_item.default_schedule_config is not None:
        return watched_item.default_schedule_config
    if watched_item.domain_default_schedule_config is not None:
        return watched_item.domain_default_schedule_config
    return SYSTEM_DEFAULT_SCHEDULE_CONFIG


def resolved_schedule_config(watched_item: WatchedItem) -> dict:
    """Resolve a WatchedItem's schedule config: the 4-tier chain under the floor.

    Returns a fresh dict when the floor applies, so a caller cannot mutate the
    stored column through the returned value.
    """
    config = base_schedule_config(watched_item)

    floor = watched_item.throttle_floor_interval
    if not floor:
        return config

    try:
        resolved_is_faster = parse_interval(config.get("interval")) < parse_interval(floor)
    except ValueError:
        # The reconcile validates before storing, so an unparseable interval
        # should be unreachable here — but this is the scheduler's hot loop and
        # a raise would kill the tick for every item. The floor is the safe
        # direction: slower, never faster.
        resolved_is_faster = True

    if not resolved_is_faster:
        return config
    return {**config, "interval": floor}
