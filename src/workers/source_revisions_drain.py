"""The ``content.revisions`` producer: drains ``pending_archiver_sync`` to the bus.

Watcher emits an **observation** — "the content extracted from this InfoSource now
has this fingerprint" — and Archiver decides what to persist (cannobserv#301,
CannObserv/archiver#139). Watcher never commands the registry, and no
``source_revision_id`` travels: a service that does not own the registry mints no
registry ids.

**The outbox stays.** It is the producer-side durability guarantee, exactly what
the cluster strategy asks of a producer with a database; only the transport moved
from ``POST /source-revisions`` to an XADD. Delivery is fire-and-forget past the
publish: the envelope key ``info_source_id:extracted_fingerprint`` matches
Archiver's uniqueness constraint, so at-least-once redelivery is an idempotent
no-op there.

**Two failure classes, and conflating them is the bug this file is shaped to
avoid.** Building the payload is pure, so any failure is *deterministic* —
identical every loop — and the row dead-letters at once rather than spinning
forever. Publishing can fail because the broker is down, which is *transient*:
retry indefinitely, exempt from any ceiling, because an outage is not the row's
fault and a data-loss cliff at attempt N would discard real revisions. Mirrors
Archiver's own producer split (``src/core/changes/publisher.py``).
"""

from datetime import UTC, datetime

from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import to_wire
from co_core.pure.models.changes import SourceRevisionObservedEmit
from co_core_aio.bus import AsyncBusPublisher
from redis.exceptions import BusyLoadingError, OutOfMemoryError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.core.bus import BUS_REDIS_URL_ENV, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.change_revision import ChangeRevision
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.watched_item import WatchedItem
from src.core.sources.outbox import dead_letter, delete_pending, mark_failure, select_due
from src.workers import bp

logger = get_logger(__name__)

# A broker that is unreachable, loading, or out of memory recovers on its own.
# Anything else that escapes the publish call is treated as non-transient and
# counts toward the backstop ceiling below.
_TRANSIENT_PUBLISH_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,  # builtin
    TimeoutError,  # builtin
    RedisConnectionError,
    RedisTimeoutError,
    BusyLoadingError,
    OutOfMemoryError,
)

# Backstop only, for a non-transient publish error that somehow persists. Set
# absurdly high on purpose: the ceiling exists so a genuinely stuck row is
# eventually visible, not as a retry budget. Deterministic failures never reach
# it — they dead-letter on the first pass.
MAX_PUBLISH_ATTEMPTS = 100_000

# Distinguishes "caller passed no client, use the shared one" from "caller
# passed None, meaning no bus" — which a plain None default cannot express, and
# which the no-bus guard below depends on.
_UNSET: object = object()


def _build_emit(
    row: PendingArchiverSync,
    rev: ChangeRevision,
    watched_item: WatchedItem,
) -> SourceRevisionObservedEmit:
    """The wire payload, sourced from the row rather than recomputed.

    ``extracted_fingerprint`` is ``rev.content_fingerprint`` **verbatim**, prefix
    included: Archiver enforces ``^sha256:[0-9a-f]{64}$`` and treats a violation
    as poison, and an unprefixed value would write a row that can never
    idempotently match the prefixed one for identical content — a silent
    duplicate rather than a loud reject.

    Not the blob's fingerprint. ``BlobAvailableEvent.content_fingerprint`` is
    Replicator's sha256 of the raw bytes; this is sha256 of the text extracted
    under ``source_specs``. Different inputs, different services, never
    cross-matched.

    Raises ``ValidationError`` when the row lacks a wire-required value — the
    caller dead-letters, because no amount of retrying will add it.
    """
    return SourceRevisionObservedEmit(
        occurred_at=datetime.now(UTC),
        info_source_id=watched_item.archiver_info_source_id,
        extracted_fingerprint=rev.content_fingerprint,
        captured_at=rev.captured_at,
        content_size_bytes=rev.content_size_bytes,
        content_media_type=row.content_media_type,
        source_media_type=row.source_media_type,
        blob_uri=row.blob_uri,
        blob_expires_at=row.blob_expires_at,
        command_id=row.command_id,
        spec_fingerprint=row.spec_fingerprint,
    )


@bp.periodic(cron="* * * * *", periodic_id="drain_pending_archiver_sync")
@bp.task(name="drain_pending_archiver_sync", queue="default")
async def drain_pending_archiver_sync(
    *, batch_size: int = 100, bus_client: object = _UNSET, **periodic_kwargs
) -> dict:
    """Publish due outbox rows to ``content.revisions``; drop each on success."""
    client = get_shared_bus_client() if bus_client is _UNSET else bus_client
    if client is None:
        logger.warning(
            "bus not configured — source revisions stay queued",
            extra={"env": BUS_REDIS_URL_ENV},
        )
        return {"skipped": "no_bus"}

    publisher = AsyncBusPublisher(client)
    published = failed = dead_lettered = 0
    session_factory = get_session_factory()

    async with session_factory() as session:
        rows = await select_due(session, limit=batch_size)
        for row in rows:
            rev = await session.get(ChangeRevision, row.change_revision_id)
            if rev is None:
                logger.error(
                    "drain: ChangeRevision not found, dropping row",
                    extra={
                        "pending_id": str(row.id),
                        "change_revision_id": str(row.change_revision_id),
                    },
                )
                await delete_pending(session, row.id)
                continue

            # archiver_info_source_id is NOT NULL since #251, so the only
            # remaining orphan case is the WatchedItem being deleted after
            # select_due read this batch — reachable only across concurrent
            # transactions, since the pending row is ON DELETE CASCADE.
            watched_item = await session.get(WatchedItem, row.watched_item_id)
            if watched_item is None:
                logger.error(
                    "drain: WatchedItem missing, dropping row",
                    extra={"pending_id": str(row.id), "watched_item_id": str(row.watched_item_id)},
                )
                await delete_pending(session, row.id)
                continue

            # Build phase — pure, so any failure repeats identically. Caught
            # broadly on purpose: the guarantee is "no build error spins
            # forever", which a narrower except cannot make.
            try:
                fields = to_wire(_build_emit(row, rev, watched_item))
            except Exception as exc:
                row.attempts += 1
                await dead_letter(session, row, error=repr(exc), reason="unbuildable_payload")
                dead_lettered += 1
                logger.error(
                    "drain: unpublishable observation, dead-lettering",
                    extra={
                        "pending_id": str(row.id),
                        "watched_item_id": str(row.watched_item_id),
                        "error": str(exc),
                    },
                )
                continue

            # Publish phase — at-least-once boundary. If the process dies between
            # the XADD and the commit, the row republishes next drain; safe only
            # because the envelope key makes redelivery a no-op for Archiver.
            try:
                await publisher.execute(BusPublish(streams.CONTENT_REVISIONS, fields))
            except Exception as exc:
                transient = isinstance(exc, _TRANSIENT_PUBLISH_ERRORS)
                if not transient and row.attempts + 1 >= MAX_PUBLISH_ATTEMPTS:
                    # mark_failure owns the counter on the retry branch; this
                    # branch is terminal, so it increments for itself.
                    row.attempts += 1
                    await dead_letter(session, row, error=repr(exc), reason="attempts_exhausted")
                    dead_lettered += 1
                else:
                    await mark_failure(session, row, error=f"{type(exc).__name__}: {exc}")
                    failed += 1
                    logger.warning(
                        "drain: publish failed",
                        extra={
                            "pending_id": str(row.id),
                            "attempts": row.attempts,
                            "transient": transient,
                            "error": str(exc),
                        },
                    )
                continue

            await delete_pending(session, row.id)
            published += 1

        await session.commit()

    if published or failed or dead_lettered:
        logger.info(
            "drain_pending_archiver_sync finished",
            extra={
                "published": published,
                "failed": failed,
                "dead_lettered": dead_lettered,
            },
        )
    return {"published": published, "failed": failed, "dead_lettered": dead_lettered}
