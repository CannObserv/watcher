"""Periodic Procrastinate task publishing the watch-status full set (#264).

The cron republish is the load-bearing half of the last-write-wins contract
(cannobserv#321): a consumer's boot replay must never depend on broker
retention, so the whole set — every WatchedItem plus every tombstone — goes
out every tick. Mutation paths (reconcile, health transition, active/cadence
change) additionally defer this same task so a state change reaches Archiver's
panel in seconds, not at the next tick; the two paths share one publish
function, so they cannot drift.

**The republish period is the recovery bound.** A dropped frame (this stream
has no outbox — loss is accepted because the next full set corrects it, in a
way it never could be for a registry mutation) leaves Archiver's panel stale
until the next tick, so the period is deploy-tunable via
``WATCHER_WATCH_STATUS_REPUBLISH_CRON`` (default every 5 minutes) — chosen
against panel staleness, and independent of the announcement stream's period.
"""

import os

from croniter import croniter
from procrastinate.exceptions import AlreadyEnqueued

from src.core.bus import bus_disabled_reason, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.watch_status import publish_full_status_set
from src.workers import bp

logger = get_logger(__name__)

REPUBLISH_CRON_ENV = "WATCHER_WATCH_STATUS_REPUBLISH_CRON"
DEFAULT_REPUBLISH_CRON = "*/5 * * * *"


def _republish_cron() -> str:
    """The republish cadence, from env — falling back loudly on a bad value.

    Read at import (the cron is a decorator argument), so a change needs a
    restart. A malformed expression must degrade to the default cadence, never
    kill the worker at import or silence the stream.
    """
    cron = os.environ.get(REPUBLISH_CRON_ENV, DEFAULT_REPUBLISH_CRON)
    if not croniter.is_valid(cron):
        logger.error(
            "invalid %s %r — falling back to %r",
            REPUBLISH_CRON_ENV,
            cron,
            DEFAULT_REPUBLISH_CRON,
        )
        return DEFAULT_REPUBLISH_CRON
    return cron


@bp.periodic(cron=_republish_cron(), periodic_id="publish_watch_status")
@bp.task(name="publish_watch_status", queue="default")
async def publish_watch_status(**periodic_kwargs) -> dict:
    """Publish every WatchedItem's scheduler state (and every tombstone) to the bus.

    Skips loudly when no bus client can be built — ``WATCHER_BUS_REDIS_URL``
    unset, or set without the ``WATCHER_BUS_ENABLED=1`` opt-in (#262): nothing
    in Watcher blocks on this stream — it must never become an ack path — but
    Archiver's panel and drift detector go stale, and an operator must be able
    to see that the statuses are not travelling. A publish failure raises —
    Procrastinate records the failed job, and the next cron tick is the retry.
    """
    # Asked of src.core.bus, not of the URL variable: since #262 the URL alone
    # builds no client, so a bare env check here would pass and then hit the
    # assert below on a None client — or, under python -O, publish on None.
    reason = bus_disabled_reason()
    if reason is not None:
        logger.error(
            "watch-status publish skipped: %s — Archiver's panel and drift detector are stale",
            reason,
        )
        return {"skipped": reason}

    # Shared, lifespan-owned client (#241 CR-4) — never closed here.
    client = get_shared_bus_client()
    assert client is not None  # guarded by bus_disabled_reason() above
    async with get_session_factory()() as session:
        published = await publish_full_status_set(session, client)
    logger.info("watch-status full set published", extra={"published": published})
    return {"published": published}


async def defer_status_republish() -> None:
    """Defer a full-set republish (called by mutation paths, post-commit).

    Best-effort by design: the mutation has already committed, and the
    periodic tick republishes everything anyway — so a failed defer degrades
    to at most one period of staleness. It must never fail the request or
    reconcile that triggered it.

    The queueing lock coalesces bursts: a cold-start reconcile applies N
    announcements back-to-back, and without it each would queue its own job,
    each publishing the entire set (N² frames at scale — CR-2). At most one
    republish waits in the queue; the publisher reads committed rows, so the
    one that runs carries everything the burst wrote.
    """
    try:
        await publish_watch_status.configure(queueing_lock="publish_watch_status").defer_async()
    except AlreadyEnqueued:
        # A republish is already queued and will read this mutation's committed
        # state when it runs — the burst coalesced, which is the design.
        logger.debug("watch-status republish already queued — coalesced")
    except Exception:
        logger.warning(
            "could not defer watch-status republish; the periodic tick will cover it",
            exc_info=True,
        )
