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
* A fact whose processing keeps failing is acked past the same way, after
  ``MAX_DELIVERY_ATTEMPTS`` (#389; ``run_fact_consumer``), instead of blocking
  every fact behind it. Its fetch is the reaper's to re-issue.

Duplicate facts (redelivery, or Replicator's crash-window re-emit) re-run the
upsert and re-defer the apply task; the apply task's status guard makes the
second run a no-op — at-least-once ends at the row, exactly-once at the apply.
"""

import asyncio
from typing import Protocol

import psycopg
from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.exceptions import BusMessageAnomaly
from co_core.pure.adapters.bus.streams import group_name
from co_core.pure.models.changes import BlobAvailableEvent, FetchFailedEvent
from co_core_aio.bus import AsyncBusConsumer
from redis import exceptions as redis_errors
from redis.asyncio import Redis
from sqlalchemy import exc as sa_exc
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
# Attempts at one message before the loop retires it (#389): dead-lettered on the
# command inbox, acked past on a fact inbox. Five at the 5 s backoff is ~20 s of
# retrying an error that is not a connection error. A constant until someone
# needs to tune it.
MAX_DELIVERY_ATTEMPTS = 5

# The dependency is unreachable, so every message fails alike: not an attempt.
# ``InterfaceError`` is absent on purpose — asyncpg raises it for session-state
# misuse ("another operation is in progress"), which recurs on every attempt.
_CONNECTION_ERRORS = (
    OSError,  # ConnectionRefusedError, ConnectionResetError, builtin TimeoutError
    sa_exc.OperationalError,
    # Procrastinate's pool, under the defer that follows a fact's commit; it
    # arrives as the cause of a ``ConnectorException``. Includes ``PoolTimeout``.
    psycopg.OperationalError,
    redis_errors.ConnectionError,
    redis_errors.TimeoutError,
)
# Postgres refusing service, by SQLSTATE — on asyncpg, psycopg and SQLAlchemy's
# translated errors alike. Needed because asyncpg's connect raises these raw:
# SQLAlchemy neither translates nor flags them, so while the server restarts every
# lazy connect inside ``process`` raises ``CannotConnectNowError`` (CR 6). Class
# 08 is connection exception, 53 insufficient resources (slots, disk, memory);
# 57P01–57P03 are admin shutdown, crash shutdown, cannot connect now. 57014
# (a statement timeout) is absent: it can be the message's own fault.
_UNAVAILABLE_SQLSTATE_CLASSES = ("08", "53")
_UNAVAILABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


def _is_unavailable_sqlstate(exc: BaseException) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    if not isinstance(sqlstate, str):
        return False
    return sqlstate in _UNAVAILABLE_SQLSTATES or sqlstate[:2] in _UNAVAILABLE_SQLSTATE_CLASSES


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


def is_connection_error(exc: BaseException) -> bool:
    """Whether ``exc`` says a dependency is unreachable rather than that this
    message cannot be processed (#389).

    Walks ``__cause__``, so a wrapped one is still recognised. Anything not
    recognised counts as an attempt: misclassifying a transient costs one
    retired message, misclassifying a deterministic failure a stuck inbox.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _CONNECTION_ERRORS):
            return True
        if isinstance(current, sa_exc.DBAPIError) and current.connection_invalidated:
            return True
        if _is_unavailable_sqlstate(current):
            return True
        current = current.__cause__
    return False


async def _dead_letter(
    consumer: AsyncBusConsumer,
    topic: str,
    message_id: str,
    fields,
    *,
    reason: str,
    command_id: str | None = None,
) -> None:
    """Route one frame to ``<topic>.dlq`` (which acks it), and log it.

    The one writer for both causes — undecodable, and out of attempts (#389) —
    which is what ``tests/test_bus_stream_kinds.py`` pins. ``reason`` names an
    exception class only: the DLQ is readable by anything with DLQ access, and
    an error's text can quote the payload. ``command_id`` is ``None`` for an
    undecodable frame, which has no payload to read it from.
    """
    dlq_id = await consumer.dead_letter(message_id, dict(fields), reason=reason)
    logger.error(
        "%s frame dead-lettered",
        topic,
        extra={
            "message_id": message_id,
            "command_id": command_id,
            "dlq_id": dlq_id,
            "reason": reason,
        },
    )


async def _claim_dead_lettering_poison(consumer: AsyncBusConsumer, topic: str) -> list[BusMessage]:
    """Reclaim our pending entries; route each undecodable one to the DLQ."""
    page = await consumer.claim_stale_page(min_idle_ms=0, count=10)
    for frame in page.poison:
        await _dead_letter(
            consumer,
            topic,
            frame.message_id,
            frame.fields,
            reason=f"undecodable: {type(frame.anomaly).__name__}",
        )
    return list(page.messages)


async def _retire(
    consumer: AsyncBusConsumer,
    topic: str,
    message: BusMessage,
    *,
    attempts: int,
    error: str,
    dead_letter: bool,
) -> None:
    """Stop retrying a message that failed ``attempts`` times (#389).

    The command inbox dead-letters it; a fact inbox acks it past, its policy for
    an undecodable frame too — the reaper re-issues the command whose fact never
    landed (MUST-6). A failure here propagates to the loop's backoff, and the
    next pass retires it again without re-running ``process``.

    Both log lines name the ``command_id``: on a fact inbox the journal is the
    only record of the drop, and on the command inbox it is what Archiver keys on.
    """
    reason = f"failed {attempts} attempts: {error}"
    command_id = getattr(message.payload, "command_id", None)
    if dead_letter:
        await _dead_letter(
            consumer,
            topic,
            message.message_id,
            message.fields,
            reason=reason,
            command_id=command_id,
        )
        return
    await consumer.ack(message.message_id)
    logger.error(
        "%s message acked past",
        topic,
        extra={"message_id": message.message_id, "command_id": command_id, "reason": reason},
    )


class ProcessFn(Protocol):
    """One decoded fact → an outcome tag; commits before the loop acks."""

    async def __call__(self, session, message: BusMessage) -> str: ...


async def run_fact_consumer(
    consumer: AsyncBusConsumer,
    session_factory,
    *,
    stop: asyncio.Event,
    topic: str,
    process: ProcessFn,
    block_ms: int = BLOCK_MS,
    error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
    group_start_id: str = "$",
    dead_letter_poison: bool = False,
) -> None:
    """Poll → process → ack on one grouped stream, until ``stop`` is set.

    The loop every grouped inbox runs — ``content.blobs`` here,
    ``content.derived`` (``src/workers/derived_facts.py``, #325) and the
    ``info.watch-check`` command inbox (``src/workers/watch_check.py``, #371) —
    so the error handling below exists once: a second copy is how a later fix
    reaches only one of the paths that needs it (see ``_back_off``). Each
    caller builds its own ``AsyncBusConsumer`` from a streams constant and a
    ``group_name`` binding, because ``tests/test_bus_stream_kinds.py`` reads
    exactly that call site; ``topic`` only labels the log lines.

    ``group_start_id`` defaults to ``"$"`` for the fact inboxes: facts
    published before our group existed predate any command Watcher issued and
    can never correlate. A command inbox passes ``"0"`` — a command issued
    before the group existed is still owed an answer. After a crash, our own
    unacked entries come back via ``claim_stale`` (a same-name consumer does
    NOT re-see its PEL on ``>`` reads).

    ``dead_letter_poison`` is the command inbox's policy for an undecodable
    frame: it goes to ``<topic>.dlq`` (``AsyncBusConsumer.dead_letter``, which
    acks it) instead of being acked past. ``read`` raises without the raw
    fields, so the frame is left in our PEL and the very next pass reclaims it
    through ``claim_stale_page``, which hands poison back with its fields.

    **A message that keeps failing is retired** (#389): once ``process`` has
    raised on it ``MAX_DELIVERY_ATTEMPTS`` times, the next pass dead-letters it
    (``dead_letter_poison``) or acks it past, without processing it again.
    Before #389 it was reclaimed forever and blocked every message behind it.
    Only ``process`` raising counts, and not with a connection error
    (``is_connection_error``): an outage fails every message alike. The count
    lives in this loop, so a restart resets it — co-core reports no delivery
    count, and the server's needs an ``XPENDING`` grant nobody has checked.

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
    loop = asyncio.get_running_loop()
    next_claim = loop.time()  # first pass drains any crash leftovers immediately
    group_ready = False  # created inside the guard: a broker outage racing our
    # boot must back off and retry, not kill the task before the loop starts (CR-12)
    # message_id → (attempts that counted, the last one's exception class). An
    # entry leaves on ack or retirement, so it holds only messages now failing.
    failures: dict[str, tuple[int, str]] = {}

    while not stop.is_set():
        try:
            if not group_ready:
                await consumer.ensure_group(start_id=group_start_id)
                group_ready = True
            messages: list[BusMessage] = []
            if loop.time() >= next_claim:
                next_claim = loop.time() + CLAIM_INTERVAL_SECONDS
                if dead_letter_poison:
                    messages = await _claim_dead_lettering_poison(consumer, topic)
                else:
                    messages = await consumer.claim_stale(min_idle_ms=0, count=10)
            if not messages:
                messages = await consumer.read(count=1, block_ms=block_ms)

            if not messages:
                # A client that ignores `block` would busy-spin without this.
                await asyncio.sleep(IDLE_SLEEP_SECONDS)
                continue

            for message in messages:
                attempts, error = failures.get(message.message_id, (0, ""))
                if attempts >= MAX_DELIVERY_ATTEMPTS:
                    await _retire(
                        consumer,
                        topic,
                        message,
                        attempts=attempts,
                        error=error,
                        dead_letter=dead_letter_poison,
                    )
                    del failures[message.message_id]
                    continue
                try:
                    async with session_factory() as session:
                        outcome = await process(session, message)
                except Exception as exc:
                    if not is_connection_error(exc):
                        failures[message.message_id] = (attempts + 1, type(exc).__name__)
                    raise
                await consumer.ack(message.message_id)
                failures.pop(message.message_id, None)
                logger.info(
                    "%s message processed",
                    topic,
                    extra={"message_id": message.message_id, "outcome": outcome},
                )
        except BusMessageAnomaly as exc:
            message_id = getattr(exc, "message_id", None)
            if dead_letter_poison:
                # Left pending: the next pass's claim returns it with its raw
                # fields, which is what the dead letter copies.
                logger.warning(
                    "undecodable frame on %s — dead-lettering on the next claim",
                    topic,
                    extra={"message_id": message_id, "error": str(exc)},
                )
                next_claim = loop.time()
                continue
            # Undecodable frame: ack past it (see module docstring).
            logger.warning(
                "undecodable frame on %s — skipping",
                topic,
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
                        extra={"message_id": message_id, "topic": topic},
                        exc_info=True,
                    )
                    next_claim = loop.time()
                    await _back_off(stop, error_backoff_seconds)
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("%s consumer error — backing off", topic, exc_info=True)
            # An unacked in-process message should come back promptly, not
            # after the full claim interval.
            next_claim = loop.time()
            await _back_off(stop, error_backoff_seconds)
            continue


async def run_blobs_consumer(
    client: Redis,
    session_factory,
    *,
    stop: asyncio.Event,
    block_ms: int = BLOCK_MS,
    error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
) -> None:
    """The ``content.blobs`` inbox: :func:`run_fact_consumer` over ``process_fact_message``."""
    await run_fact_consumer(
        AsyncBusConsumer(
            client, topic=streams.CONTENT_BLOBS, group=CONSUMER_GROUP, consumer=CONSUMER_NAME
        ),
        session_factory,
        stop=stop,
        topic=streams.CONTENT_BLOBS,
        # Resolved per message, not bound here, so a test can patch the module
        # attribute under a running loop.
        process=lambda session, message: process_fact_message(session, message),
        block_ms=block_ms,
        error_backoff_seconds=error_backoff_seconds,
    )


def watch_consumer_task(task: asyncio.Task, *, stop: asyncio.Event, topic: str) -> asyncio.Task:
    """Attach the dead-man's switch to a fact-consumer task (CR-1).

    The lifespan never awaits these tasks until shutdown, so an escaped
    exception would otherwise kill a fact inbox silently while the process
    keeps serving.
    """

    def _observe(t: asyncio.Task) -> None:
        if t.cancelled() or stop.is_set():
            return  # orderly shutdown
        exc = t.exception()
        if exc is not None:
            logger.critical(
                "%s consumer task DIED — facts will pile up in the PEL until restart",
                topic,
                exc_info=exc,
            )
        else:
            logger.critical("%s consumer task exited unexpectedly", topic)

    task.add_done_callback(_observe)
    return task


def start_blobs_consumer(client: Redis, session_factory, *, stop: asyncio.Event) -> asyncio.Task:
    """Spawn the consumer loop as a lifespan task (caller owns client + stop)."""
    task = asyncio.create_task(run_blobs_consumer(client, session_factory, stop=stop))
    return watch_consumer_task(task, stop=stop, topic=streams.CONTENT_BLOBS)
