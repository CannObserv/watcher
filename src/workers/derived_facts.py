"""The content.derived consumer — the processing leg's fact inbox (#325).

One consumer group, ``watcher.derived`` (derived, never written), one member —
the single-process topology. It runs on ``fetch_facts.run_fact_consumer``, the
loop the ``content.blobs`` inbox runs on, so ack-past for an undecodable frame,
the backoff guard and the PEL reclaim are the same code.

Per message, correlate on ``command_id`` only — ``content.derived`` is
broadcast, so a fact for a command Watcher did not issue is discarded:

* ``ProcessingCompleteEvent`` → write the fact fields, settle the row
  ``COMPLETED``, commit, defer ``apply_process_fact``. An ``empty`` outcome is a
  result, not a failure (D5): the apply decides whether it chains the next spec.
* ``ProcessingFailedEvent`` → branch ``terminal`` first. Terminal settles the
  row ``FAILED`` and defers the apply; non-terminal (``transient``) only
  refreshes ``fact_at``, the reaper's signal that the processor is alive.

**The first terminal fact wins** (CannObserv/processor#17). A lost ack makes
the processor publish the same outcome again under a fresh ``occurred_at`` —
its envelope key is ``command_id:occurred_at``, so the bus never merges the
two — and a give-up after three escaping exceptions publishes a failure that
may follow a success it could not ack. So a fact for a row already settled is
logged and dropped; deduplicating on the envelope key, or letting the apply
compare fingerprints, would let the duplicate through. A fact for a row the
reaper expired is late and dropped likewise: its lineage has moved on.
"""

import asyncio

from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.streams import group_name
from co_core.pure.models.changes import ProcessingCompleteEvent, ProcessingFailedEvent
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis

from src.core.logging import get_logger
from src.core.models.process_command import (
    SETTLED_PROCESS_STATUSES,
    ProcessCommand,
    ProcessCommandStatus,
)
from src.core.utils import format_utc_iso
from src.workers.fetch_facts import (
    BLOCK_MS,
    ERROR_BACKOFF_SECONDS,
    DeferFn,
    run_fact_consumer,
    watch_consumer_task,
)
from src.workers.process_commands import apply_process_fact

logger = get_logger(__name__)

# `<service>.<stream-suffix>` through the helper (#285, cannobserv#384).
CONSUMER_GROUP = group_name(streams.CONTENT_DERIVED, "watcher")
# Group-derived with the dot flattened, like `watcher-blobs-1`.
CONSUMER_NAME = "watcher-derived-1"


async def _defer_apply(command_id: str) -> None:
    await apply_process_fact.configure().defer_async(command_id=command_id)


def _check_echo(row: ProcessCommand, payload) -> None:
    """Warn when the echoed ``info_source_id`` disagrees — reporting, never routing."""
    if payload.info_source_id != row.info_source_id:
        logger.warning(
            "info_source_id echo mismatch — correlating on command_id anyway",
            extra={
                "command_id": row.command_id,
                "commanded": row.info_source_id,
                "echoed": payload.info_source_id,
            },
        )


async def process_derived_message(
    session, message: BusMessage, *, defer: DeferFn | None = None
) -> str:
    """Settle one decoded fact onto its row; returns an outcome tag.

    Commits before the caller acks: the row is the durable record, the ack only
    the PEL release. A crash between the two redelivers the fact, which then
    finds its row settled and is dropped — the apply was already deferred.
    """
    defer = defer if defer is not None else _defer_apply
    payload = message.payload
    if not isinstance(payload, ProcessingCompleteEvent | ProcessingFailedEvent):
        logger.info(
            "unexpected payload type on content.derived — ignoring",
            extra={"event_type": getattr(payload, "event_type", "?")},
        )
        return "ignored_unknown_type"

    # Locked (CR 2): the reaper may be expiring this row right now. Whichever
    # commits first, the other sees its outcome — never both acting on it.
    row = await session.get(ProcessCommand, payload.command_id, with_for_update=True)
    if row is None:
        # Broadcast stream: another issuer's command, or a row since deleted
        # with its watched item. Nothing of ours to settle.
        logger.info(
            "derived fact matched no process command — discarding",
            extra={
                "command_id": payload.command_id,
                "info_source_id": payload.info_source_id,
                "event_type": payload.event_type,
            },
        )
        return "unmatched"
    _check_echo(row, payload)

    terminal = isinstance(payload, ProcessingCompleteEvent) or payload.terminal
    if row.status in SETTLED_PROCESS_STATUSES or row.status == ProcessCommandStatus.EXPIRED:
        if terminal:
            logger.info(
                "derived fact for a settled process command — the first terminal fact stands",
                extra={
                    "command_id": row.command_id,
                    "status": row.status,
                    "event_type": payload.event_type,
                    "occurred_at": format_utc_iso(payload.occurred_at),
                },
            )
        return "late" if row.status == ProcessCommandStatus.EXPIRED else "already_settled"

    row.fact_at = payload.occurred_at
    if isinstance(payload, ProcessingCompleteEvent):
        row.status = ProcessCommandStatus.COMPLETED
        row.empty = payload.empty
        row.output_digest = payload.output_digest
        row.output_uri = payload.output_uri
        row.output_size_bytes = payload.output_size_bytes
        row.output_media_type = payload.output_media_type
        row.spec_fingerprint = payload.spec_fingerprint
        row.spec_schema_version = payload.spec_schema_version
        row.processor_version = payload.processor_version
        await session.commit()
        await defer(row.command_id)
        return "complete_recorded"

    if not payload.terminal:
        # The processor chose to surface a retry: it is alive and on it.
        await session.commit()
        return "nonterminal_recorded"
    row.status = ProcessCommandStatus.FAILED
    row.failure_reason = payload.reason
    row.failure_detail = payload.detail
    await session.commit()
    await defer(row.command_id)
    return "failure_recorded"


async def run_derived_consumer(
    client: Redis,
    session_factory,
    *,
    stop: asyncio.Event,
    block_ms: int = BLOCK_MS,
    error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
) -> None:
    """The ``content.derived`` inbox: ``run_fact_consumer`` over ``process_derived_message``."""
    await run_fact_consumer(
        AsyncBusConsumer(
            client, topic=streams.CONTENT_DERIVED, group=CONSUMER_GROUP, consumer=CONSUMER_NAME
        ),
        session_factory,
        stop=stop,
        topic=streams.CONTENT_DERIVED,
        # Resolved per message so a test can patch the module's defer seam.
        process=lambda session, message: process_derived_message(
            session, message, defer=_defer_apply
        ),
        block_ms=block_ms,
        error_backoff_seconds=error_backoff_seconds,
    )


def start_derived_consumer(client: Redis, session_factory, *, stop: asyncio.Event) -> asyncio.Task:
    """Spawn the consumer loop as a lifespan task (caller owns client + stop)."""
    task = asyncio.create_task(run_derived_consumer(client, session_factory, stop=stop))
    return watch_consumer_task(task, stop=stop, topic=streams.CONTENT_DERIVED)
