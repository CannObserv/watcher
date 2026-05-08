"""Drain unpublished Changes from the outbox to the Redis bus.

Phase 2c envelope (schema_version 2) carries info_item_id, info_spec_id,
and previous/current fingerprints, and partitions the stream by
info_item_id (was watch_id in Phase 2b's v1 shape).

A PostgreSQL transaction-scoped advisory lock guards the drain so manual
invocations and the cron-driven schedule can't double-publish: only one
holder of ``DRAIN_ADVISORY_LOCK_ID`` proceeds; others log and exit early.
The lock auto-releases at transaction end.

Two drainers run concurrently in production:

* The 1-minute Procrastinate periodic ``drain_changes_outbox`` is the
  safety-net floor. It survives if the fast loop's task crashes.
* The fast async loop (#144) ticks every
  ``CHANGES_DRAIN_INTERVAL_SECONDS`` seconds (default 10) inside the
  Watcher process. Sub-minute end-to-end latency from
  ``Change.detected_at`` to Redis publish.

Both share ``_drain_changes_once`` and the same advisory lock, so they
can't double-publish.
"""

import asyncio
import json
import os

import sqlalchemy as sa

from src.core.changes.outbox import mark_published, select_unpublished
from src.core.changes.publisher import ChangePublisher
from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.utils import format_utc_iso
from src.workers import bp

logger = get_logger(__name__)

INFO_CHANGES_TOPIC = "info.changes"

# Transaction-scoped advisory lock guarding the drain. Shared lock space with
# session-scoped ``pg_advisory_lock`` — a session-level holder blocks the
# transaction-level acquirer here. Constant chosen for Phase 2c; grep src/ for
# ``pg_advisory`` before reusing this id elsewhere.
DRAIN_ADVISORY_LOCK_ID = 0xCDA1

# Fast-tick async drain loop cadence. Sub-minute target so end-to-end
# Change-detected -> Redis-publish latency stays low; the 1-minute cron
# stays in place as a safety floor.
DEFAULT_DRAIN_INTERVAL_SECONDS = 10
_DRAIN_INTERVAL_ENV_VAR = "CHANGES_DRAIN_INTERVAL_SECONDS"


def _resolve_drain_interval() -> int:
    """Resolve ``CHANGES_DRAIN_INTERVAL_SECONDS`` with safe fallback.

    Invalid (non-numeric, zero, or negative) values fall back to
    ``DEFAULT_DRAIN_INTERVAL_SECONDS`` and log a warning. The fast loop
    must always have a positive cadence — a zero interval would
    busy-loop the event loop.
    """
    raw = os.environ.get(_DRAIN_INTERVAL_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_DRAIN_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid CHANGES_DRAIN_INTERVAL_SECONDS, using default",
            extra={"raw": raw, "default": DEFAULT_DRAIN_INTERVAL_SECONDS},
        )
        return DEFAULT_DRAIN_INTERVAL_SECONDS
    if value <= 0:
        logger.warning(
            "non-positive CHANGES_DRAIN_INTERVAL_SECONDS, using default",
            extra={"raw": raw, "default": DEFAULT_DRAIN_INTERVAL_SECONDS},
        )
        return DEFAULT_DRAIN_INTERVAL_SECONDS
    return value


def _build_envelope(change) -> bytes:
    """Build the JSON wire envelope for a Change row.

    Phase 2c shape (schema_version 2)::

        {
          "schema_version": 2,
          "change_id": "<ULID>",
          "watch_id": "<ULID>",
          "info_item_id": "<ULID>",
          "info_spec_id": "<ULID>",
          "previous_snapshot_id": "<ULID> | null",
          "current_snapshot_id": "<ULID>",
          "previous_fingerprint": <int | null>,
          "current_fingerprint": <int | null>,
          "detected_at": "<ISO8601 UTC>",
          "significance": <float | null>,
          "visual_change_score": <float | null>,
          "metadata": <dict>,
        }
    """
    return json.dumps(
        {
            "schema_version": 2,
            "change_id": str(change.id),
            "watch_id": str(change.watch_id),
            "info_item_id": str(change.info_item_id),
            "info_spec_id": str(change.info_spec_id),
            "previous_snapshot_id": (
                str(change.previous_snapshot_id) if change.previous_snapshot_id else None
            ),
            "current_snapshot_id": str(change.current_snapshot_id),
            "previous_fingerprint": change.previous_fingerprint,
            "current_fingerprint": change.current_fingerprint,
            "detected_at": format_utc_iso(change.detected_at),
            "significance": change.significance,
            "visual_change_score": change.visual_change_score,
            "metadata": change.change_metadata,
        }
    ).encode("utf-8")


async def _drain_changes_once(
    *, batch_size: int = 100, publisher: ChangePublisher | None = None
) -> dict:
    """Run a single drain pass; shared by the periodic and the fast loop.

    Returns a dict with ``published``, ``failed``, and
    ``skipped_due_to_lock``. Idempotent — only processes rows where
    ``published_to_bus_at IS NULL``. Per-row errors are caught and counted
    in ``failed``; the rest of the batch continues.

    Single-writer: a transaction-scoped advisory lock
    (``DRAIN_ADVISORY_LOCK_ID``) prevents concurrent drains from
    double-publishing. Concurrent invocations return
    ``{"published": 0, "failed": 0, "skipped_due_to_lock": True}``.

    Publisher ownership: if ``publisher`` is supplied the caller owns it
    (used by the fast loop to amortize the Redis client across ticks,
    #154). Otherwise this function constructs and closes one per call —
    preserving the per-tick lifecycle of the 1-minute periodic worker.
    """
    owned_publisher = publisher is None
    if publisher is None:
        publisher = ChangePublisher()
    published = 0
    failed = 0
    try:
        async with get_session_factory()() as session:
            locked = await session.scalar(
                sa.select(sa.func.pg_try_advisory_xact_lock(DRAIN_ADVISORY_LOCK_ID))
            )
            if not locked:
                logger.info("drain_changes_outbox skipped — another drain holds the lock")
                return {"published": 0, "failed": 0, "skipped_due_to_lock": True}
            rows = await select_unpublished(session, limit=batch_size)
            for change in rows:
                try:
                    payload = _build_envelope(change)
                    msg_id = await publisher.publish_change(
                        topic=INFO_CHANGES_TOPIC,
                        # Phase 2c partitions by info_item_id (was watch_id in v1).
                        key=str(change.info_item_id),
                        payload=payload,
                        headers={"schema_version": "2"},
                    )
                    await mark_published(session, change.id, bus_message_id=msg_id)
                    published += 1
                except Exception as e:
                    logger.exception(
                        "change drain failed for row",
                        extra={"change_id": str(change.id), "error": str(e)},
                    )
                    failed += 1
            await session.commit()
    finally:
        if owned_publisher:
            await publisher.aclose()
    return {"published": published, "failed": failed, "skipped_due_to_lock": False}


@bp.periodic(cron="* * * * *", periodic_id="drain_changes_outbox")
@bp.task(name="drain_changes_outbox", queue="default")
async def drain_changes_outbox(*, batch_size: int = 100, **periodic_kwargs) -> dict:
    """Periodic 1-minute safety-net drain.

    Stays in place even with the fast async loop: if the fast loop's task
    dies (asyncio crash, lifespan ordering bug), the cron still drains
    every minute. The advisory lock guarantees the two never
    double-publish.

    Returns ``{"published": N, "failed": M}`` (legacy shape — keeping
    for any callers that introspect the periodic's return). Skipped runs
    additionally include ``"skipped": True`` for backwards compatibility
    with prior tests.
    """
    result = await _drain_changes_once(batch_size=batch_size)
    if result["skipped_due_to_lock"]:
        return {"published": 0, "failed": 0, "skipped": True}
    logger.info(
        "drain_changes_outbox finished",
        extra={"published": result["published"], "failed": result["failed"]},
    )
    return {"published": result["published"], "failed": result["failed"]}


async def start_changes_drain_loop(
    interval: int | None = None,
) -> asyncio.Task:
    """Start the fast-tick async drain loop; return its ``asyncio.Task``.

    The loop ticks every ``interval`` seconds (default resolved from
    ``CHANGES_DRAIN_INTERVAL_SECONDS``, falling back to
    ``DEFAULT_DRAIN_INTERVAL_SECONDS``). Each tick calls
    ``_drain_changes_once`` and logs structured per-tick counts. Errors
    inside the drain are logged but never kill the loop — the periodic
    cron remains as a floor either way.

    Cancellation semantics: when the returned task is cancelled,
    ``CancelledError`` propagates into ``_drain_changes_once``'s
    ``async with get_session_factory()()`` block, which rolls back the
    transaction. Rows that were XADD'd to Redis but not yet
    ``mark_published``'d may therefore be republished by the next tick
    or by the 1-minute periodic. This is bounded by the advisory lock
    (no concurrent drains) and the ``published_to_bus_at IS NULL``
    filter (already-marked rows are skipped). The next tick does not
    start once cancellation is observed.
    """
    resolved_interval = interval if interval is not None else _resolve_drain_interval()
    logger.info(
        "starting fast-tick changes drain loop",
        extra={"interval_seconds": resolved_interval},
    )

    async def _loop() -> None:
        # Loop-owned publisher reused across ticks (#154). Built lazily on
        # first xadd via ChangePublisher's `_get_client`. Closed once on
        # shutdown via the finally below.
        publisher = ChangePublisher()
        try:
            while True:
                try:
                    result = await _drain_changes_once(publisher=publisher)
                    logger.info(
                        "fast drain tick",
                        extra={
                            "published": result["published"],
                            "failed": result["failed"],
                            "skipped_due_to_lock": result["skipped_due_to_lock"],
                        },
                    )
                except asyncio.CancelledError:
                    # Re-raise so the task ends on shutdown without being
                    # smothered by the broad except below.
                    raise
                except Exception:
                    logger.warning("fast drain tick raised; will retry next tick", exc_info=True)
                await asyncio.sleep(resolved_interval)
        finally:
            await publisher.aclose()

    return asyncio.create_task(_loop(), name="changes_drain_fast_loop")
