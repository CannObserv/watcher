"""Periodic Procrastinate task publishing the fetch-policy full set (#245).

The cron republish is the load-bearing half of the last-write-wins contract
(cannobserv#285): a consumer's boot replay must never depend on broker
retention, so the whole set — every Domain plus every tombstone — goes out
every tick. Mutation paths (domain create/edit/delete) additionally defer this
same task so an operator change reaches Replicator in seconds, not at the next
quarter-hour; the two paths share one publish function, so they cannot drift.
"""

from src.core.bus import bus_disabled_reason, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.fetch_policy import publish_full_policy_set
from src.core.logging import get_logger
from src.workers import bp

logger = get_logger(__name__)


@bp.periodic(cron="*/5 * * * *", periodic_id="publish_fetch_policy")
@bp.task(name="publish_fetch_policy", queue="default")
async def publish_fetch_policy(**periodic_kwargs) -> dict:
    """Publish every Domain's politeness numbers (and every tombstone) to the bus.

    Skips loudly when no bus client can be built — ``WATCHER_BUS_REDIS_URL``
    unset, or set without the ``WATCHER_BUS_ENABLED=1`` opt-in (#262):
    Replicator falls back to its conservative per-host default, which is safe,
    but an operator must be able to see that the numbers are not travelling. A
    publish failure raises — Procrastinate records the failed job, and the next
    cron tick is the retry.
    """
    # Asked of src.core.bus, not of the URL variable: since #262 the URL alone
    # builds no client, so a bare env check here would pass and then hit the
    # assert below on a None client — or, under python -O, publish on None.
    reason = bus_disabled_reason()
    if reason is not None:
        logger.error(
            "fetch-policy publish skipped: %s — Replicator is pacing every host at its own default",
            reason,
        )
        return {"skipped": reason}

    # Shared, lifespan-owned client (#241 CR-4) — never closed here.
    client = get_shared_bus_client()
    assert client is not None  # guarded by bus_disabled_reason() above
    async with get_session_factory()() as session:
        published = await publish_full_policy_set(session, client)
    logger.info("fetch-policy full set published", extra={"published": published})
    return {"published": published}


async def defer_policy_republish() -> None:
    """Defer a full-set republish (called by domain mutation paths, post-commit).

    Best-effort by design: the mutation has already committed, and the periodic
    tick republishes everything anyway — so a failed defer degrades to at most
    ~15 minutes of staleness. It must never fail the request that triggered it.
    """
    try:
        await publish_fetch_policy.configure().defer_async()
    except Exception:
        logger.warning(
            "could not defer fetch-policy republish; the periodic tick will cover it",
            exc_info=True,
        )
