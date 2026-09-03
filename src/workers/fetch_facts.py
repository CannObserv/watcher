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
from collections.abc import Mapping
from typing import Literal, Protocol

from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.exceptions import BusMessageAnomaly
from co_core.pure.adapters.bus.streams import group_name
from co_core.pure.models.changes import BlobAvailableEvent, FetchFailedEvent
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select

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
# The pre-#285 name. Renamed in place by `migrate_legacy_group` on first boot;
# delete this and that function once every deployment has booted past it.
LEGACY_CONSUMER_GROUP = "watcher"
# One member: the single-process topology is load-bearing (AGENTS.md). A second
# process would need its own consumer name AND a shared apply-ordering story.
# Group-derived with the dot flattened: `watcher.blobs-1` would read as the
# `-<purpose>` group form. Host-independent and restart-stable, which is the
# property the #285 audit checked and the only one that matters here.
CONSUMER_NAME = "watcher-blobs-1"

# The outcomes of `migrate_legacy_group`, named rather than restated at each
# site (CR-17). The loop branches on the classification below, and the dangerous
# direction is silent: a refusal misclassified as settled clears the retry timer
# and reverts CR-12 with a green suite. `test_every_outcome_is_classified_...`
# reads the function's own `return`s and fails if one is missing from either set.
MIGRATED = "migrated"
NO_LEGACY_GROUP = "no_legacy_group"
LEGACY_PEL_NOT_DRAINED = "legacy_pel_not_drained"
NEW_GROUP_AHEAD_OF_LEGACY = "new_group_ahead_of_legacy"
NEW_GROUP_CREATED_CONCURRENTLY = "new_group_created_concurrently"

MigrationOutcome = Literal[
    "migrated",
    "no_legacy_group",
    "legacy_pel_not_drained",
    "new_group_ahead_of_legacy",
    "new_group_created_concurrently",
]

# Nothing outstanding: the consumer never re-attempts.
SETTLED_MIGRATION_OUTCOMES = frozenset({MIGRATED, NO_LEGACY_GROUP})
# A standing condition the consumer keeps re-attempting — see `run_blobs_consumer`.
# Every one of these implies `watcher.blobs` already exists; that is the invariant
# the ordering rests on (CR-18), pinned by
# `test_no_unsettled_outcome_leaves_the_new_group_absent`.
UNSETTLED_MIGRATION_OUTCOMES = frozenset(
    {LEGACY_PEL_NOT_DRAINED, NEW_GROUP_AHEAD_OF_LEGACY, NEW_GROUP_CREATED_CONCURRENTLY}
)
MIGRATION_OUTCOMES = SETTLED_MIGRATION_OUTCOMES | UNSETTLED_MIGRATION_OUTCOMES

# A refusal is re-attempted after `CLAIM_INTERVAL_SECONDS`, then at double the
# previous wait while the outcome is unchanged, clamped here (CR-19). Fixed-rate
# retries would log the ahead-of-legacy ERROR 1440 times a day for a condition
# that needs a human; a changed outcome resets to the floor.
MIGRATION_RETRY_CEILING_SECONDS = 3600.0
# Bound on the overlap XRANGE behind the replay warning: the count is there to
# size the replay for an operator, not to be exact about a pathological one.
REPLAY_COUNT_CAP = 1000

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


def _group_text(group: Mapping[str, object], key: str) -> str:
    """One ``XINFO GROUPS`` field as text — the client decodes or it doesn't.

    redis-py returns ``str`` keys with ``bytes`` values unless the client was
    built with ``decode_responses`` (verified against redis-py 7.4.1 and the
    live broker); fakeredis matches. ``str()`` on the fallback keeps the return
    type honest for the numeric fields rather than passing an ``int`` through a
    signature that promises text.
    """
    value = group[key]
    return value.decode() if isinstance(value, bytes) else str(value)


def _stream_position(group: Mapping[str, object]) -> tuple[int, int]:
    """A group's ``last-delivered-id`` as a sortable ``(ms, seq)`` pair.

    Stream ids are ``<ms>-<seq>`` in decimal, so comparing them as strings is
    wrong in the direction that matters: ``"9-0" > "10-0"`` lexicographically
    while ``9 < 10`` numerically. Every comparison here goes through this.
    """
    ms, _, seq = _group_text(group, "last-delivered-id").partition("-")
    return int(ms), int(seq or 0)


async def migrate_legacy_group(client: Redis) -> MigrationOutcome:
    """Rename the pre-#285 bare ``watcher`` group to ``CONSUMER_GROUP`` (#285).

    **Why this is code and not a runbook step.** ``ensure_group`` mints at
    ``start_id="$"``. A renamed Watcher that restarts *before* the broker-side
    ``XGROUP CREATE`` therefore starts reading at the tail, and every fact
    published between the legacy group's last read and that moment is delivered
    to nobody — no error, no PEL, no lag, no signal of any kind. The correct
    ordering (create at the old position, *then* restart) was a hand-typed pair
    of ``redis-cli`` commands guarding against undetectable data loss; doing it
    in-process makes it deterministic, testable, and independent of the
    CannObserv/broker#1 Phase 3 window.

    Idempotent by construction — once the legacy group is gone every subsequent
    boot takes the ``no_legacy_group`` path. It must run *before*
    ``ensure_group``: that creating the new group at ``$`` first would leave no
    legacy position to inherit is the whole hazard, restated one caller up.

    **Invariant, load-bearing (CR-18): every unsettled outcome implies
    ``CONSUMER_GROUP`` already exists on the broker.** The caller runs this once
    before ``ensure_group`` and then re-attempts on a timer, so from the second
    attempt onward an ``ensure_group(start_id="$")`` has already happened in
    between; it is a ``BUSYGROUP`` no-op only because of this. An unsettled
    return with the group *absent* — a stricter guard added above the
    ``xgroup_create``, say — would let ``ensure_group`` mint it at the tail on
    the very pass that refused, which is #285's original data loss reintroduced
    through the fix for it. Pinned by
    ``test_no_unsettled_outcome_leaves_the_new_group_absent``.

    **Nothing is destroyed while it is the only record of a position.** Two
    guards, and both refuse rather than repair, because repair means choosing
    between a gap and a replay and that is an operator's call:

    * *The new group is already ahead of the legacy one.* Reachable by hand —
      ``XGROUP CREATE … $`` instead of the recorded id — and destroying the
      legacy group would then drop every entry between them **and** the only
      evidence of where it was. Compared numerically (see ``_stream_position``).
    * *The legacy PEL is not drained.* ``XGROUP DESTROY`` discards the entries
      in it. The audit read ``pending 0`` on 2026-09-01; that is a reading, not
      a property of the deploy window.

    Both leave the legacy group standing and say so loudly, and the caller keeps
    re-attempting while they stand, so a drained PEL completes the rename with
    no restart (CR-12). So does losing the create race: a group made by someone
    else sits at a position this function did not choose, so it declines to
    assert an inheritance it cannot vouch for and re-evaluates on the next
    attempt, where the comparison above applies.

    The remaining case — the new group **behind** the legacy one — completes,
    because the overlap merely re-reads and the consumer is idempotent at the
    row (MUST-4). It still warns: it is the one path that changes what the
    process does next, and an operator watching a burst of redelivered facts is
    owed the reason.
    """
    try:
        groups = await client.xinfo_groups(streams.CONTENT_BLOBS)
    except ResponseError as exc:
        # Only "no such key" means *no stream*. A WRONGTYPE, or the NOPERM that
        # arrives once broker#1 D7 puts a credential in front of this call, is a
        # failed question — not the answer "nothing to migrate". Raising is safe:
        # the caller's backoff guard retries without killing the consumer.
        # redis-py exposes no typed exception for this, so the text is the only
        # discriminator; verified against Redis 7.0.15 / redis-py 7.4.1 (CR-14).
        # The stub in the tests pins the same literal, so a server-side
        # rephrasing would fail loudly in production, not quietly here.
        if "no such key" not in str(exc).lower():
            raise
        return NO_LEGACY_GROUP

    by_name = {_group_text(g, "name"): g for g in groups}
    legacy = by_name.get(LEGACY_CONSUMER_GROUP)
    if legacy is None:
        return NO_LEGACY_GROUP

    existing = by_name.get(CONSUMER_GROUP)
    if existing is None:
        last_delivered = _group_text(legacy, "last-delivered-id")
        try:
            # No mkstream: reaching here means the legacy group exists, which
            # means the stream does.
            await client.xgroup_create(streams.CONTENT_BLOBS, CONSUMER_GROUP, id=last_delivered)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            logger.warning(
                "content.blobs group appeared mid-rename — keeping the legacy group; "
                "its position is compared on the next retry",
                extra={"old_group": LEGACY_CONSUMER_GROUP, "new_group": CONSUMER_GROUP},
            )
            return NEW_GROUP_CREATED_CONCURRENTLY
        logger.info(
            "content.blobs consumer group renamed (#285)",
            extra={
                "old_group": LEGACY_CONSUMER_GROUP,
                "new_group": CONSUMER_GROUP,
                "inherited_last_delivered_id": last_delivered,
            },
        )
    elif _stream_position(existing) > _stream_position(legacy):
        logger.error(
            "content.blobs group %s is AHEAD of the legacy group — refusing to destroy it; "
            "facts between the two positions would be delivered to nobody. Repoint "
            "%s with XGROUP SETID (replaying the overlap) or destroy the legacy group to "
            "accept the gap — no restart needed, this is re-checked on a retry",
            CONSUMER_GROUP,
            CONSUMER_GROUP,
            extra={
                "old_group": LEGACY_CONSUMER_GROUP,
                "new_group": CONSUMER_GROUP,
                "legacy_last_delivered_id": _group_text(legacy, "last-delivered-id"),
                "new_last_delivered_id": _group_text(existing, "last-delivered-id"),
            },
        )
        return NEW_GROUP_AHEAD_OF_LEGACY
    elif _stream_position(existing) < _stream_position(legacy):
        # Safe — the overlap re-reads and the consumer is idempotent at the row
        # (MUST-4) — but it is the one path that materially changes behaviour,
        # so it must not also be the one path that says nothing. Counting via
        # XRANGE rather than the groups' `entries-read`, which is None for a
        # group created by XGROUP CREATE and so unusable exactly here.
        overlap = await client.xrange(
            streams.CONTENT_BLOBS,
            min=f"({_group_text(existing, 'last-delivered-id')}",
            max=_group_text(legacy, "last-delivered-id"),
            count=REPLAY_COUNT_CAP,
        )
        logger.warning(
            "content.blobs group %s is BEHIND the legacy group — completing the rename, "
            "which replays the overlap; the apply guard makes each redelivery a no-op",
            CONSUMER_GROUP,
            extra={
                "old_group": LEGACY_CONSUMER_GROUP,
                "new_group": CONSUMER_GROUP,
                "legacy_last_delivered_id": _group_text(legacy, "last-delivered-id"),
                "new_last_delivered_id": _group_text(existing, "last-delivered-id"),
                "replayed_entries": len(overlap),
                "replayed_entries_capped": len(overlap) >= REPLAY_COUNT_CAP,
            },
        )

    if legacy["pending"]:
        logger.warning(
            "legacy content.blobs group still has unacked entries — keeping it; "
            "drain the PEL and the next retry completes the #285 rename, no restart needed",
            extra={"old_group": LEGACY_CONSUMER_GROUP, "pending": legacy["pending"]},
        )
        return LEGACY_PEL_NOT_DRAINED

    await client.xgroup_destroy(streams.CONTENT_BLOBS, LEGACY_CONSUMER_GROUP)
    return MIGRATED


async def run_blobs_consumer(
    client: Redis,
    session_factory,
    *,
    stop: asyncio.Event,
    block_ms: int = BLOCK_MS,
    error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
) -> None:
    """Poll → process → ack, until ``stop`` is set.

    ``migrate_legacy_group`` first (#285): it is what keeps a rename from
    silently skipping the facts published while the old-named consumer was down.
    Its refusals are **standing conditions**, so an unsettled outcome is
    re-attempted after ``CLAIM_INTERVAL_SECONDS`` and then at doubling waits up
    to ``MIGRATION_RETRY_CEILING_SECONDS`` until it settles — a drained PEL
    completes the rename with no restart, and the ahead-of-legacy ERROR keeps
    reappearing while it remains true instead of scrolling away once at boot,
    without logging it 1440 times a day (CR-12, CR-19). A settled outcome
    retries never; a *changed* outcome resets the wait to the floor.

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
    next_migration_retry: float | None = None  # set only while a refusal stands
    retry_delay = CLAIM_INTERVAL_SECONDS
    last_migration_outcome: str | None = None
    group_ready = False  # created inside the guard: a broker outage racing our
    # boot must back off and retry, not kill the task before the loop starts (CR-12)

    async def _attempt_migration() -> float | None:
        """Run the migration; return when to retry it, or ``None`` once settled.

        Unchanged refusals back off geometrically to a ceiling so a condition
        needing a human stays visible without flooding the log; a *changed*
        outcome resets to the floor, because it means the situation moved and
        the next step deserves to be seen promptly (CR-19).
        """
        nonlocal retry_delay, last_migration_outcome
        outcome = await migrate_legacy_group(client)
        settled = outcome in SETTLED_MIGRATION_OUTCOMES
        if not settled:
            retry_delay = (
                CLAIM_INTERVAL_SECONDS
                if outcome != last_migration_outcome
                else min(retry_delay * 2, MIGRATION_RETRY_CEILING_SECONDS)
            )
        last_migration_outcome = outcome
        return None if settled else loop.time() + retry_delay

    while not stop.is_set():
        try:
            if not group_ready:
                # Before ensure_group, never after: creating the new group at
                # "$" first would leave no legacy position to inherit (#285).
                next_migration_retry = await _attempt_migration()
                await consumer.ensure_group(start_id="$")
                group_ready = True
            if next_migration_retry is not None and loop.time() >= next_migration_retry:
                # A refusal is a standing condition, not a boot-time note: an
                # operator draining the PEL exactly as the log says should not
                # also need a restart, and the ahead-of-legacy ERROR must keep
                # reappearing while it is true rather than scrolling away once.
                next_migration_retry = await _attempt_migration()
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
