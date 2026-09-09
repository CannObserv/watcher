"""The content.blobs consumer — Watcher's fact inbox for Phase 4 (#241).

One consumer group (``watcher.blobs`` — fact streams broadcast, one group per
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
  non-terminal only refreshes ``fact_at``. The one exception is
  ``reason="not_modified"`` (#249): a 304 is a *successful check that found no
  change*, so a terminal fact carrying it closes the row as ``NOT_MODIFIED`` and
  defers ``apply_fetch_not_modified`` instead — it must never reach
  ``apply_fetch_failure``, which would mark a healthy item ERROR and notify a
  user about it on every no-change check.
* Unknown ``command_id`` → ack and drop, with a log line. By contract the
  in-flight fact for a lost map entry "will arrive, match nothing, and have to
  be discarded" — this is that discard. Since cannobserv#300 the line also names
  the WatchedItem the fact's ``info_source_id`` resolves to, when it resolves to
  one; see ``_log_orphan`` for why that is reporting and not recovery.
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
from co_core.pure.adapters.bus.streams import group_name
from co_core.pure.models.changes import BlobAvailableEvent, FetchFailedEvent
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis
from sqlalchemy import select

from src.core import read_windows
from src.core.logging import get_logger
from src.core.models.fetch_command import (
    NOT_MODIFIED_REASON,
    FetchCommand,
    FetchCommandStatus,
)
from src.core.models.watched_item import WatchedItem
from src.workers.fetch_commands import (
    apply_fetch_blob,
    apply_fetch_failure,
    apply_fetch_not_modified,
)

logger = get_logger(__name__)

# Derived, never hand-written (#285, cannobserv#384): `<service>.<stream-suffix>`,
# no `purpose` segment because Watcher runs exactly one group on this stream. The
# 0/5 cluster-wide conformance rate #384 documents was the product of a convention
# that lived in prose beside a free-string `group` parameter — so the helper is the
# contract, and it raises on a config/state topic that must never grow a group.
CONSUMER_GROUP = group_name(streams.CONTENT_BLOBS, "watcher")
# One member: the single-process topology is load-bearing (AGENTS.md). A second
# process would need its own consumer name AND a shared apply-ordering story.
# Group-derived with the dot flattened: `watcher.blobs-1` would read as the
# `-<purpose>` group form. Host-independent and restart-stable, which is the
# property the #285 audit checked and the only one that matters here.
CONSUMER_NAME = "watcher-blobs-1"

# Read block per poll; also the shutdown latency ceiling. Owned by the leaf
# module so `src.core.bus` can derive its socket_timeout from the longest
# window without importing this one (#287) — re-exported here because this is
# where a reader of the loop looks for it.
BLOCK_MS = read_windows.BLOBS_BLOCK_MS
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


async def _defer_apply_not_modified(command_id: str) -> None:
    await apply_fetch_not_modified.configure().defer_async(command_id=command_id)


async def _log_orphan(
    session,
    message: str,
    payload: BlobAvailableEvent | FetchFailedEvent,
    **fields,
) -> None:
    """Report a fact that correlates to no command of ours — attributably (#252).

    ``info_source_id`` (cannobserv#300) makes the discard *attributable*, not
    recoverable. ``content.blobs`` is broadcast, so a fact naming one of our
    InfoSources may answer another issuer's command entirely — and its bytes were
    fetched under that issuer's User-Agent, which fingerprints are sensitive to
    (see ``WATCHER_USER_AGENT``). Applying it would manufacture a change signal.
    So the discard stands and the field buys a line an operator can act on: the
    WatchedItem it *would* concern, when the id resolves to one.
    """
    # ``first()``, not ``scalar_one_or_none()``: nothing constrains one
    # WatchedItem per InfoSource, and a second row must not turn a log line into
    # a raise — that would leave the message unacked and re-read forever.
    watched_item_id = (
        (
            await session.execute(
                select(WatchedItem.id)
                .where(WatchedItem.archiver_info_source_id == payload.info_source_id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    logger.warning(
        message,
        extra={
            "command_id": payload.command_id,
            "info_source_id": payload.info_source_id,
            "url": payload.url,
            "watched_item_id": str(watched_item_id) if watched_item_id is not None else None,
            **fields,
        },
    )


def _check_echo(row: FetchCommand, payload: BlobAvailableEvent | FetchFailedEvent) -> None:
    """Warn when a fact's echoed ``info_source_id`` disagrees with the command's.

    Free (both values are in hand) and an integrity signal on the round-trip —
    but never grounds to refuse the fact: ``command_id`` is the correlator
    (MUST-3), and the command's own snapshot is the authority on what was asked.
    """
    if payload.info_source_id != row.info_source_id:
        logger.warning(
            "info_source_id echo mismatch — correlating on command_id anyway",
            extra={
                "command_id": row.command_id,
                "commanded": row.info_source_id,
                "echoed": payload.info_source_id,
            },
        )


async def process_fact_message(
    session,
    message: BusMessage,
    *,
    defer_blob: DeferFn = _defer_apply_blob,
    defer_failure: DeferFn = _defer_apply_failure,
    defer_not_modified: DeferFn = _defer_apply_not_modified,
) -> str:
    """Apply one decoded fact to the pending map; returns an outcome tag.

    Commits before the caller acks — the row is the durable record, the ack is
    only the PEL release. Crash between commit and ack → redelivery re-runs the
    upsert (idempotent) and re-defers (guarded).
    """
    payload = message.payload

    if isinstance(payload, BlobAvailableEvent):
        row = await session.get(FetchCommand, payload.command_id)
        if row is None:
            await _log_orphan(
                session,
                "blob fact matched no fetch command — discarding",
                payload,
                content_fingerprint=payload.content_fingerprint,
            )
            return "unmatched"
        _check_echo(row, payload)
        if row.applied_at is not None:
            # Late duplicate after the apply already ran; the row's outcome is
            # settled — refresh nothing, change nothing.
            return "already_applied"
        row.fact_at = payload.occurred_at
        row.content_fingerprint = payload.content_fingerprint
        row.blob_uri = payload.blob_uri
        row.blob_expires_at = payload.blob_expires_at
        row.size_bytes = payload.size_bytes
        row.media_type = payload.media_type
        row.content_type_raw = payload.content_type_raw
        row.final_url = payload.final_url
        row.status_code = payload.status_code
        # #269: the conditional-GET validators, verbatim. Recorded on the row as
        # provenance for this occasion; the item-level pair the next command
        # replays is written by the apply path, after its ordering guard.
        row.etag = payload.etag
        row.last_modified = payload.last_modified
        await session.commit()
        await defer_blob(row.command_id)
        return "blob_recorded"

    if isinstance(payload, FetchFailedEvent):
        row = await session.get(FetchCommand, payload.command_id)
        if row is None:
            await _log_orphan(
                session,
                "failure fact matched no fetch command — discarding",
                payload,
                reason=payload.reason,
            )
            return "unmatched"
        _check_echo(row, payload)
        if row.applied_at is not None:
            return "already_applied"
        row.fact_at = payload.occurred_at
        if not payload.terminal:
            # Visibility only (none emitted today — replicator#9 §3): the
            # command is still retrying; fact_at keeps the reaper's hands off.
            await session.commit()
            return "nonterminal_recorded"
        if payload.reason == NOT_MODIFIED_REASON:
            # #249: not a failure. Close the command under its own status and
            # send it down the success-shaped apply. ``failure_reason`` stays
            # NULL on purpose — at steady state this token outnumbers every real
            # failure combined (co-core's own note), so journalling it as one
            # would destroy ``failure_reason`` as a signal. The status column is
            # the record. No fingerprint and no ``blob_uri`` are written either:
            # there are no bytes for this occasion, and ``content_fingerprint``
            # here is Replicator's raw-bytes identity for *this* command.
            row.status = FetchCommandStatus.NOT_MODIFIED
            if payload.status_code is not None:
                row.status_code = payload.status_code
            await session.commit()
            await defer_not_modified(row.command_id)
            return "not_modified_recorded"
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


async def _back_off(stop: asyncio.Event, seconds: float) -> None:
    """Park for ``seconds``, cut short by shutdown.

    Extracted so the two error handlers in :func:`run_blobs_consumer` cannot
    drift: one of them was added by #289, and a copy-pasted second wait is how a
    later change to the backoff reaches only one of the paths that needs it.

    **It covers two of the three copies in this repo.** ``run_registry_consumer``
    still has the same wait inline (``src/workers/registry_reconcile.py``), and
    the argument above applies to it verbatim — it is left alone only because
    sharing this across the two worker modules is a wider change than #289.
    Anyone touching the backoff should change that one too, or finish the
    extraction.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


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

    The undecodable-frame ack needs **its own** guard to satisfy that (#289).
    It runs in a sibling ``except`` clause, so the handler below is structurally
    unable to catch it: unguarded, an undecodable frame plus any broker blip on
    the ack killed the task, and the two halves are individually ordinary. The
    registry loop's equivalent branch needs no such guard — it advances a local
    cursor (``seek``) rather than issuing a command — so do not add one there
    for symmetry.
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
                try:
                    await consumer.ack(message_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # This ack is the one fallible step that sits *outside* the
                    # guard below — it runs in a sibling `except`, so that
                    # handler cannot catch it (#289). Unguarded, an undecodable
                    # frame plus any broker blip on the ack killed the task and
                    # the fact inbox with it. Backing off here restores the
                    # docstring's invariant; the frame stays unacked, is
                    # re-read, and re-raises this branch to try again.
                    logger.warning(
                        "could not ack an undecodable frame — backing off",
                        extra={"message_id": message_id},
                        exc_info=True,
                    )
                    next_claim = loop.time()
                    await _back_off(stop, error_backoff_seconds)
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("content.blobs consumer error — backing off", exc_info=True)
            # An unacked in-process message should come back promptly, not
            # after the full claim interval.
            next_claim = loop.time()
            await _back_off(stop, error_backoff_seconds)
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
