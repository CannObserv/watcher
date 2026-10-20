"""The ``info.watch-check`` consumer — Archiver's "check this item now" (#371).

One consumer group, ``watcher.watch-check`` (derived, never written), one
member, the single-process topology. It runs on ``fetch_facts.run_fact_consumer``
like the two fact inboxes, with the command stream's two differences:

* **The group is created at ``0``**, never ``$`` (CannObserv/broker#93): a
  command Archiver issued before this group existed is still owed an answer.
* **An undecodable command is dead-lettered** to ``info.watch-check.dlq``,
  which belongs to Watcher — it writes, audits (``XLEN``/``XRANGE``) and
  ``XDEL``\\ s it — rather than acked past.

Per command (``WatchCheckCommand``, cannobserv ``docs/change_bus/watch.md``):

* **Already accepted** — a fetch row names this ``command_id`` → ``duplicate``,
  nothing done. A crash between the commit and the ack redelivers the command,
  and its fetch is open: pre-flight alone would refuse a running check as
  ``fetch_open``. A redelivered *rejection* is pre-flighted again; the contract
  lets that mint a second ``event_id``, and Archiver closes by ``command_id``.
* **Refused** — no item, or check-now's pre-flight (``src.core.check_now``)
  says no → a ``check_rejected`` fact through #369's outbox, committed here.
  The vocabulary is closed and has no archived token: an archived item is
  inactive, so it is ``paused`` (operator decision on #371). ``unknown_item``
  is the one reason that names no ``info_source_id``.
* **Accepted** — a forced full fetch, issued inline through the same path as
  ``check_watched_item``, its row carrying ``check_command_id``. That path
  re-reads the open-command gate; a scheduled check that committed after
  pre-flight makes it skip, and the command is rejected ``fetch_open``. There is no
  success fact: the outcome is a level on ``info.watch-status``, and the fetch's
  completion republishes it whatever the outcome
  (``src.workers.fetch_commands.republish_status``).

Nobody issues to this stream until F4 (#376); Watcher holds group read on it
and cannot ``XADD`` there.
"""

import asyncio
from datetime import UTC, datetime

from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.streams import group_name
from co_core.pure.models.changes import CheckRejectionReason, WatchCheckCommand
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.check_now import CheckRefusal, preflight_check
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.fetch_command import FetchCommand
from src.core.models.watched_item import WatchedItem
from src.core.watch_events import enqueue_check_rejected
from src.workers.fetch_facts import (
    BLOCK_MS,
    ERROR_BACKOFF_SECONDS,
    run_fact_consumer,
    watch_consumer_task,
)
from src.workers.tasks import issue_fetch_command

logger = get_logger(__name__)

# `<service>.<stream-suffix>` through the helper (#285, cannobserv#384).
CONSUMER_GROUP = group_name(streams.INFO_WATCH_CHECK, "watcher")
# Group-derived with the dot flattened, like `watcher-blobs-1`.
CONSUMER_NAME = "watcher-watch-check-1"
# Broker's rule (CannObserv/broker#93): never `$` against a stream that may
# already hold commands.
GROUP_START_ID = "0"

#: Check-now's refusal → the wire's closed ``CheckRejectionReason``.
REJECTION_REASONS: dict[CheckRefusal, CheckRejectionReason] = {
    CheckRefusal.ARCHIVED: "paused",
    CheckRefusal.PAUSED: "paused",
    CheckRefusal.DOMAIN_SUSPENDED: "domain_suspended",
    CheckRefusal.NO_URL: "no_url",
    CheckRefusal.FETCH_OPEN: "fetch_open",
}


async def _already_accepted(session: AsyncSession, command_id: str) -> bool:
    stmt = select(FetchCommand.command_id).where(FetchCommand.check_command_id == command_id)
    return (await session.execute(stmt.limit(1))).first() is not None


async def _item_for(session: AsyncSession, info_item_id: str) -> WatchedItem | None:
    """The WatchedItem linked to ``info_item_id``; ``None`` for an id that is
    not a ULID — no validator on the command (#283), so any string decodes."""
    try:
        key = ULID.from_str(info_item_id)
    except ValueError:
        return None
    stmt = select(WatchedItem).where(WatchedItem.archiver_info_item_id == key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _reject(
    session: AsyncSession,
    command: WatchCheckCommand,
    reason: CheckRejectionReason,
    *,
    info_source_id: str | None,
) -> str:
    enqueue_check_rejected(
        session,
        info_item_id=command.info_item_id,
        info_source_id=info_source_id,
        command_id=command.command_id,
        reason=reason,
        now=datetime.now(UTC),
    )
    await session.commit()
    logger.info(
        "watch_check rejected",
        extra={
            "check_command_id": command.command_id,
            "info_item_id": command.info_item_id,
            "reason": reason,
        },
    )
    return f"rejected_{reason}"


async def process_watch_check_message(
    session: AsyncSession, message: BusMessage, *, bus_client: Redis | None
) -> str:
    """Answer one decoded command; returns an outcome tag.

    Commits before the caller acks: the rejection fact or the fetch row is the
    durable answer, the ack only the PEL release.
    """
    command = message.payload
    if not isinstance(command, WatchCheckCommand):
        logger.info(
            "unexpected payload type on info.watch-check — ignoring",
            extra={"event_type": getattr(command, "event_type", "?")},
        )
        return "ignored_unknown_type"

    if await _already_accepted(session, command.command_id):
        logger.info(
            "watch_check already accepted — redelivery, nothing to do",
            extra={"check_command_id": command.command_id},
        )
        return "duplicate"

    item = await _item_for(session, command.info_item_id)
    if item is None:
        return await _reject(session, command, "unknown_item", info_source_id=None)

    verdict = await preflight_check(session, item)
    if verdict.refusal is not None:
        return await _reject(
            session,
            command,
            REJECTION_REASONS[verdict.refusal],
            info_source_id=item.archiver_info_source_id,
        )

    audit(
        session,
        EventType.WATCHED_ITEM_CHECK_REQUESTED,
        watched_item_id=str(item.id),
        source="bus",
        check_command_id=command.command_id,
    )
    # Committed inside, with the audit above: the row is durable before XADD,
    # and a failed publish leaves it for the pending-publish sweep.
    result = await issue_fetch_command(
        session, item, bus_client, force_full_fetch=True, check_command_id=command.command_id
    )
    if result.get("skipped"):
        # CR 1: the issue path re-reads the gate, and a scheduled check that
        # committed after pre-flight shuts it. Nothing was issued, so the audit
        # goes and the command is answered like any other open fetch.
        info_source_id = item.archiver_info_source_id
        await session.rollback()
        return await _reject(session, command, "fetch_open", info_source_id=info_source_id)
    logger.info(
        "watch_check accepted",
        extra={
            "check_command_id": command.command_id,
            "watched_item_id": str(item.id),
            "fetch_command_id": result.get("issued"),
        },
    )
    return "accepted"


async def run_watch_check_consumer(
    client: Redis,
    session_factory,
    *,
    stop: asyncio.Event,
    block_ms: int = BLOCK_MS,
    error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
) -> None:
    """The ``info.watch-check`` inbox: ``run_fact_consumer`` in command mode."""
    await run_fact_consumer(
        AsyncBusConsumer(
            client, topic=streams.INFO_WATCH_CHECK, group=CONSUMER_GROUP, consumer=CONSUMER_NAME
        ),
        session_factory,
        stop=stop,
        topic=streams.INFO_WATCH_CHECK,
        # Resolved per message so a test can patch the module attribute.
        process=lambda session, message: process_watch_check_message(
            session, message, bus_client=client
        ),
        block_ms=block_ms,
        error_backoff_seconds=error_backoff_seconds,
        group_start_id=GROUP_START_ID,
        dead_letter_poison=True,
    )


def start_watch_check_consumer(
    client: Redis, session_factory, *, stop: asyncio.Event
) -> asyncio.Task:
    """Spawn the consumer loop as a lifespan task (caller owns client + stop)."""
    task = asyncio.create_task(run_watch_check_consumer(client, session_factory, stop=stop))
    return watch_consumer_task(task, stop=stop, topic=streams.INFO_WATCH_CHECK)
