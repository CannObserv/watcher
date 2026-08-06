"""Periodic sweep republishing fetch commands that never reached the bus (#241).

The second half of persist-before-publish (issuer contract MUST-2): a crash — or
a broker outage — between the row's commit and its XADD leaves
``pending_publish``, and this sweep re-publishes it **under the same
``command_id``**, which Replicator's dedupe makes idempotent. Runs every minute;
in local mode (and whenever nothing is pending) it is a no-op query.
"""

from src.core.database import get_session_factory
from src.core.fetch_commands import publish_fetch_command, select_pending_publish
from src.core.fetch_policy import BUS_REDIS_URL_ENV, bus_client_from_env
from src.core.logging import get_logger
from src.workers import bp

logger = get_logger(__name__)


@bp.periodic(cron="* * * * *", periodic_id="publish_pending_fetch_commands")
@bp.task(name="publish_pending_fetch_commands", queue="default")
async def publish_pending_fetch_commands(
    *, session=None, bus_client=None, batch_size: int = 100, **periodic_kwargs
) -> dict:
    """Republish every ``pending_publish`` fetch command (same id — dedupe-safe).

    ``session`` / ``bus_client`` are test seams; production opens its own. A
    per-row failure is logged and the row stays pending for the next tick. The
    missing-env error is raised only when there is actually work to publish —
    an idle sweep in a bus-less deployment must not spam the journal.
    """
    owns_session = session is None
    ctx = get_session_factory()() if owns_session else None
    db = await ctx.__aenter__() if owns_session else session
    try:
        rows = await select_pending_publish(db, limit=batch_size)
        if not rows:
            return {"published": 0}

        client = bus_client if bus_client is not None else bus_client_from_env()
        if client is None:
            logger.error(
                "cannot republish %d pending fetch command(s): %s is not set",
                len(rows),
                BUS_REDIS_URL_ENV,
            )
            return {"published": 0, "skipped": f"{BUS_REDIS_URL_ENV} not set"}
        owns_client = bus_client is None

        published = 0
        try:
            for row in rows:
                try:
                    await publish_fetch_command(client, row)
                    await db.commit()
                    published += 1
                except Exception:
                    # No rollback: publish raises before any ORM mutation, so
                    # the session is clean; a (rare) failed commit poisons only
                    # this task run and the next tick gets a fresh session.
                    logger.warning(
                        "fetch command republish failed; will retry next tick",
                        extra={"command_id": row.command_id},
                        exc_info=True,
                    )
        finally:
            if owns_client:
                await client.aclose()
        if published:
            logger.info("republished pending fetch commands", extra={"published": published})
        return {"published": published}
    finally:
        if owns_session:
            await ctx.__aexit__(None, None, None)
