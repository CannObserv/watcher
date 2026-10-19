"""The ``info.watch-events`` producer: drains ``pending_watch_events`` (#369).

Watcher reports its edges as facts — ``change_detected``, ``watch_error``,
``watch_recovered`` — for Archiver's ``archiver.watch-events`` group (cannobserv
``docs/change_bus/watch.md``). Each row was staged in the transaction that
recorded its edge (``src/core/watch_events.py``); this publishes it and deletes
it. The envelope key is the row's id, the ``event_id`` minted at the edge, so
an at-least-once redelivery is one fact to the consumer.

**Head-of-line order, unlike the ``content.revisions`` drain.** Rows go out in
id order and a pass stops at the first one it cannot send: a transient publish
failure, or a row still serving its backoff. Per-row backoff with a bulk clear
(#291) would let a recovery reach the stream before the error edge it closes.
The head's backoff is the whole queue's, so the rows behind it owe none, and
once it publishes they follow in the same pass.

**The same two failure classes.** Validating the payload against its Emit twin
is pure, so a refusal is deterministic: the row dead-letters at once, stays
for post-mortem, and the queue moves past it. A publish refused by the broker
(down, full, or denying ``XADD`` under its ACL — the state before
CannObserv/broker#93's grant) is transient: retried on the backoff, never
dead-lettered. ``TRANSIENT_PUBLISH_ERRORS`` is shared with that drain.

A fact stream, so the publish carries **no** ``maxlen`` (#327).
"""

from datetime import UTC, datetime

from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import to_wire
from co_core_aio.bus import AsyncBusPublisher
from redis.asyncio import Redis
from sqlalchemy import func, select

from src.core.bus import (
    BUS_REDIS_URL_ENV,
    MAX_PUBLISH_ATTEMPTS,
    TRANSIENT_PUBLISH_ERRORS,
    get_shared_bus_client,
)
from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.pending_watch_event import PendingWatchEvent
from src.core.sources.outbox import dead_letter, mark_failure
from src.core.watch_events import WATCH_EVENT_EMITS
from src.workers import bp

logger = get_logger(__name__)

# Head-of-line order holds only with one drain at a time (CR 1), so a pass
# takes this transaction-scoped advisory lock first and yields when another
# holds it — rather than SKIP LOCKED, which would step past a locked head and
# publish the rows behind it. Procrastinate's concurrency of 1 makes overlap
# unlikely today; the lock makes it impossible. The value is arbitrary, unique
# within this database ("WEVT" in ASCII).
DRAIN_LOCK_KEY = 0x57455654


def _build_fields(row: PendingWatchEvent) -> dict[str, str]:
    """The wire fields for one row; raises when its Emit refuses the payload.

    ``KeyError`` for a kind with no Emit, ``ValidationError`` for a payload the
    Emit refuses — both deterministic, so the caller dead-letters.
    """
    emit = WATCH_EVENT_EMITS[row.event_type].model_validate(row.payload)
    return to_wire(emit)


@bp.periodic(cron="* * * * *", periodic_id="drain_pending_watch_events")
@bp.task(name="drain_pending_watch_events", queue="default")
async def drain_pending_watch_events(
    *, batch_size: int = 100, bus_client: Redis | None = None, **periodic_kwargs
) -> dict:
    """Publish queued facts to ``info.watch-events`` in mint order (per item, commit order).

    ``bus_client`` is a test seam; "no bus configured" is
    ``get_shared_bus_client()`` returning ``None``, and is said only when a
    fact is waiting — an idle tick in a bus-less deployment stays quiet.
    """
    published = failed = dead_lettered = 0
    idle = {"published": 0, "failed": 0, "dead_lettered": 0}
    async with get_session_factory()() as session:
        locked = (
            await session.execute(select(func.pg_try_advisory_xact_lock(DRAIN_LOCK_KEY)))
        ).scalar_one()
        if not locked:
            return {**idle, "skipped": "drain_running"}
        rows = list(
            (
                await session.execute(
                    select(PendingWatchEvent)
                    .where(PendingWatchEvent.dead_lettered_at.is_(None))
                    .order_by(PendingWatchEvent.id)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return idle

        client = bus_client if bus_client is not None else get_shared_bus_client()
        if client is None:
            logger.warning(
                "bus not configured — watch events stay queued",
                extra={"queued": len(rows), "env": BUS_REDIS_URL_ENV},
            )
            return {**idle, "skipped": "no_bus"}
        publisher = AsyncBusPublisher(client)

        now = datetime.now(UTC)
        for row in rows:
            if row.next_attempt_at > now:
                break  # the head is backing off; everything behind it waits

            try:
                fields = _build_fields(row)
            except Exception as exc:
                # Caught broadly on purpose: no build error may wedge the queue.
                row.attempts += 1
                await dead_letter(session, row, error=repr(exc), reason="unbuildable_payload")
                dead_lettered += 1
                logger.error(
                    "drain: unpublishable watch event, dead-lettering",
                    extra={
                        "event_id": str(row.id),
                        "event_type": row.event_type,
                        "info_item_id": row.info_item_id,
                        "error": str(exc),
                    },
                )
                continue

            # At-least-once boundary: a crash between the XADD and the commit
            # republishes this row under the same key.
            try:
                await publisher.execute(BusPublish(streams.INFO_WATCH_EVENTS, fields))
            except Exception as exc:
                transient = isinstance(exc, TRANSIENT_PUBLISH_ERRORS)
                if not transient and row.attempts + 1 >= MAX_PUBLISH_ATTEMPTS:
                    row.attempts += 1
                    await dead_letter(session, row, error=repr(exc), reason="attempts_exhausted")
                    dead_lettered += 1
                    continue
                await mark_failure(session, row, error=f"{type(exc).__name__}: {exc}")
                failed += 1
                logger.warning(
                    "drain: watch event publish failed — holding the queue",
                    extra={
                        "event_id": str(row.id),
                        "attempts": row.attempts,
                        "transient": transient,
                        "error": str(exc),
                    },
                )
                break

            await session.delete(row)
            published += 1

        await session.commit()

    if published or failed or dead_lettered:
        logger.info(
            "drain_pending_watch_events finished",
            extra={"published": published, "failed": failed, "dead_lettered": dead_lettered},
        )
    return {"published": published, "failed": failed, "dead_lettered": dead_lettered}
