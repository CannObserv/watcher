"""The content.blobs consumer — Watcher's fact inbox for Phase 4 (#241).

One consumer group (``watcher`` — fact streams broadcast, one group per
service), one member (the single-process topology, see AGENTS.md). Started as a
lifespan task beside the config poller; a deployment without
``WATCHER_BUS_REDIS_URL`` simply never starts it.

Per message, branch on payload type and **correlate on ``command_id`` only**
(contract MUST-3 — ``url`` is one-to-many against InfoSources, never a key):

* ``BlobAvailableEvent`` → upsert the fact fields onto the ``fetch_commands``
  row, commit, ack, defer ``apply_fetch_blob``. Never dedupe on
  ``content_fingerprint`` (MUST-5: two commands returning identical bytes are
  two facts, same fingerprint, different command_ids — both must correlate).
* ``FetchFailedEvent`` → branch ``terminal`` first (its wire key is
  per-emission, so several distinct facts per command are normal — MUST-4);
  terminal marks the row failed and defers ``apply_fetch_failure``;
  non-terminal only refreshes ``fact_at``.
* Unknown ``command_id`` → ack and drop, with a log line. By contract the
  in-flight fact for a lost map entry "will arrive, match nothing, and have to
  be discarded" — this is that discard.
* An undecodable frame is acked past with a warning: on a fact stream we read
  with our own group there is no correlation obligation to discharge and no
  DLQ of ours to route to (mirrors Replicator's policy-reader posture).

Duplicate facts (redelivery, or Replicator's crash-window re-emit) re-run the
upsert and re-defer the apply task; the apply task's status guard makes the
second run a no-op — at-least-once ends at the row, exactly-once at the apply.
"""

import asyncio
from typing import Protocol

from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.exceptions import BusMessageAnomaly
from co_core.pure.models.changes import BlobAvailableEvent, FetchFailedEvent
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis

from src.core.logging import get_logger
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.workers.fetch_commands import apply_fetch_blob, apply_fetch_failure

logger = get_logger(__name__)

CONSUMER_GROUP = "watcher"
# One member: the single-process topology is load-bearing (AGENTS.md). A second
# process would need its own consumer name AND a shared apply-ordering story.
CONSUMER_NAME = "watcher-1"

# Read block per poll; also the shutdown latency ceiling.
BLOCK_MS = 5000
# Insurance against a client that ignores `block` (fakeredis) busy-spinning.
IDLE_SLEEP_SECONDS = 0.05
# Reclaim our own PEL this often — entries left unacked by a crash mid-process.
CLAIM_INTERVAL_SECONDS = 60.0
ERROR_BACKOFF_SECONDS = 5.0


class DeferFn(Protocol):
    """Test seam for the procrastinate defer calls."""

    async def __call__(self, command_id: str) -> None: ...


async def _defer_apply_blob(command_id: str) -> None:
    await apply_fetch_blob.configure().defer_async(command_id=command_id)


async def _defer_apply_failure(command_id: str) -> None:
    await apply_fetch_failure.configure().defer_async(command_id=command_id)


async def process_fact_message(
    session,
    message: BusMessage,
    *,
    defer_blob: DeferFn = _defer_apply_blob,
    defer_failure: DeferFn = _defer_apply_failure,
) -> str:
    """Apply one decoded fact to the pending map; returns an outcome tag.

    Commits before the caller acks — the row is the durable record, the ack is
    only the PEL release. Crash between commit and ack → redelivery re-runs the
    upsert (idempotent) and re-defers (guarded).
    """
    payload = message.payload

    if isinstance(payload, BlobAvailableEvent):
        if payload.command_id is None:
            # Non-command emit (seed tooling) — nothing of ours to correlate.
            return "ignored_non_command"
        row = await session.get(FetchCommand, payload.command_id)
        if row is None:
            logger.info(
                "blob fact matched no fetch command — discarding",
                extra={"command_id": payload.command_id, "url": payload.url},
            )
            return "unmatched"
        if row.applied_at is not None:
            # Late duplicate after the apply already ran; the row's outcome is
            # settled — refresh nothing, change nothing.
            return "already_applied"
        row.fact_at = payload.occurred_at
        row.content_fingerprint = payload.content_fingerprint
        row.blob_uri = payload.blob_uri
        row.size_bytes = payload.size_bytes
        row.media_type = payload.media_type
        row.content_type_raw = payload.content_type_raw
        row.final_url = payload.final_url
        row.status_code = payload.status_code
        await session.commit()
        await defer_blob(row.command_id)
        return "blob_recorded"

    if isinstance(payload, FetchFailedEvent):
        row = await session.get(FetchCommand, payload.command_id)
        if row is None:
            logger.info(
                "failure fact matched no fetch command — discarding",
                extra={"command_id": payload.command_id, "reason": payload.reason},
            )
            return "unmatched"
        if row.applied_at is not None:
            return "already_applied"
        row.fact_at = payload.occurred_at
        if not payload.terminal:
            # Visibility only (none emitted today — replicator#9 §3): the
            # command is still retrying; fact_at keeps the reaper's hands off.
            await session.commit()
            return "nonterminal_recorded"
        row.status = FetchCommandStatus.FAILED
        row.failure_reason = payload.reason
        row.failure_detail = payload.detail
        if payload.status_code is not None:
            row.status_code = payload.status_code
        await session.commit()
        await defer_failure(row.command_id)
        return "failure_recorded"

    logger.info(
        "unexpected payload type on content.blobs — ignoring",
        extra={"event_type": getattr(payload, "event_type", "?")},
    )
    return "ignored_unknown_type"


async def run_blobs_consumer(
    client: Redis,
    session_factory,
    *,
    stop: asyncio.Event,
    block_ms: int = BLOCK_MS,
    error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
) -> None:
    """Poll → process → ack, until ``stop`` is set.

    ``ensure_group(start_id="$")``: facts published before our group existed
    predate any command Watcher issued and can never correlate. After a crash,
    our own unacked entries come back via ``claim_stale`` (a same-name consumer
    does NOT re-see its PEL on ``>`` reads).

    **Every fallible step is inside the backoff guard** (CR-1): a transient DB
    error while processing must park-and-retry, never escape and kill the task
    — the message stays unacked, and resetting ``next_claim`` makes the next
    pass reclaim it promptly instead of waiting out the claim interval.
    """
    consumer = AsyncBusConsumer(
        client, topic=streams.CONTENT_BLOBS, group=CONSUMER_GROUP, consumer=CONSUMER_NAME
    )
    loop = asyncio.get_running_loop()
    next_claim = loop.time()  # first pass drains any crash leftovers immediately
    group_ready = False  # created inside the guard: a broker outage racing our
    # boot must back off and retry, not kill the task before the loop starts (CR-12)

    while not stop.is_set():
        try:
            if not group_ready:
                await consumer.ensure_group(start_id="$")
                group_ready = True
            messages: list[BusMessage] = []
            if loop.time() >= next_claim:
                next_claim = loop.time() + CLAIM_INTERVAL_SECONDS
                messages = await consumer.claim_stale(min_idle_ms=0, count=10)
            if not messages:
                messages = await consumer.read(count=1, block_ms=block_ms)

            if not messages:
                # A client that ignores `block` would busy-spin without this.
                await asyncio.sleep(IDLE_SLEEP_SECONDS)
                continue

            for message in messages:
                async with session_factory() as session:
                    outcome = await process_fact_message(session, message)
                await consumer.ack(message.message_id)
                logger.info(
                    "content.blobs fact processed",
                    extra={"message_id": message.message_id, "outcome": outcome},
                )
        except BusMessageAnomaly as exc:
            # Undecodable frame: ack past it (see module docstring).
            message_id = getattr(exc, "message_id", None)
            logger.warning(
                "undecodable frame on content.blobs — skipping",
                extra={"message_id": message_id, "error": str(exc)},
            )
            if message_id and message_id != "?":
                await consumer.ack(message_id)
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("content.blobs consumer error — backing off", exc_info=True)
            # An unacked in-process message should come back promptly, not
            # after the full claim interval.
            next_claim = loop.time()
            try:
                await asyncio.wait_for(stop.wait(), timeout=error_backoff_seconds)
            except TimeoutError:
                pass
            continue


def start_blobs_consumer(client: Redis, session_factory, *, stop: asyncio.Event) -> asyncio.Task:
    """Spawn the consumer loop as a lifespan task (caller owns client + stop).

    The done-callback is the dead-man's switch (CR-1): the lifespan never awaits
    this task until shutdown, so an escaped exception would otherwise kill the
    fact inbox silently while the process keeps serving.
    """
    task = asyncio.create_task(run_blobs_consumer(client, session_factory, stop=stop))

    def _observe(t: asyncio.Task) -> None:
        if t.cancelled() or stop.is_set():
            return  # orderly shutdown
        exc = t.exception()
        if exc is not None:
            logger.critical(
                "content.blobs consumer task DIED — facts will pile up in the PEL until restart",
                exc_info=exc,
            )
        else:
            logger.critical("content.blobs consumer task exited unexpectedly")

    task.add_done_callback(_observe)
    return task
