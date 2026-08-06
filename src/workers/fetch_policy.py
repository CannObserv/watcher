"""Periodic Procrastinate task publishing the fetch-policy full set (#245).

The cron republish is the load-bearing half of the last-write-wins contract
(cannobserv#285): a consumer's boot replay must never depend on broker
retention, so the whole set — every Domain plus every tombstone — goes out
every tick. Mutation paths (domain create/edit/delete) additionally defer this
same task so an operator change reaches Replicator in seconds, not at the next
quarter-hour; the two paths share one publish function, so they cannot drift.
"""

import os

from src.core.database import get_session_factory
from src.core.fetch_policy import (
    BUS_REDIS_URL_ENV,
    bus_client_from_env,
    publish_full_policy_set,
)
from src.core.logging import get_logger
from src.workers import bp

logger = get_logger(__name__)


@bp.periodic(cron="*/5 * * * *", periodic_id="publish_fetch_policy")
@bp.task(name="publish_fetch_policy", queue="default")
async def publish_fetch_policy(**periodic_kwargs) -> dict:
    """Publish every Domain's politeness numbers (and every tombstone) to the bus.

    Skips loudly when ``WATCHER_BUS_REDIS_URL`` is unset: Replicator falls back
    to its conservative per-host default, which is safe, but an operator must be
    able to see that the numbers are not travelling. A publish failure raises —
    Procrastinate records the failed job, and the next cron tick is the retry.
    """
    if not os.environ.get(BUS_REDIS_URL_ENV):
        logger.error(
            "fetch-policy publish skipped: %s is not set — Replicator is pacing "
            "every host at its own default",
            BUS_REDIS_URL_ENV,
        )
        return {"skipped": f"{BUS_REDIS_URL_ENV} not set"}

    client = bus_client_from_env()
    assert client is not None  # guarded by the env check above
    try:
        async with get_session_factory()() as session:
            published = await publish_full_policy_set(session, client)
    finally:
        await client.aclose()
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
