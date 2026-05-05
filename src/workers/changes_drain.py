"""Drain unpublished Changes from the outbox to the Redis bus.

Phase 2c envelope (schema_version 2) carries info_item_id, info_spec_id,
and previous/current fingerprints, and partitions the stream by
info_item_id (was watch_id in Phase 2b's v1 shape).

A PostgreSQL transaction-scoped advisory lock guards the drain so manual
invocations and the cron-driven schedule can't double-publish: only one
holder of ``DRAIN_ADVISORY_LOCK_ID`` proceeds; others log and exit early.
The lock auto-releases at transaction end.
"""

import json

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


@bp.periodic(cron="* * * * *", periodic_id="drain_changes_outbox")
@bp.task(name="drain_changes_outbox", queue="default")
async def drain_changes_outbox(*, batch_size: int = 100, **periodic_kwargs) -> dict:
    """Publish up to ``batch_size`` unpublished Changes; return counts.

    Idempotent — only processes rows where ``published_to_bus_at IS NULL``.
    Per-row errors are caught and counted in ``failed``; the rest of the
    batch continues. Failed rows remain unpublished and the next drain
    picks them up.

    Single-writer: a transaction-scoped advisory lock
    (``DRAIN_ADVISORY_LOCK_ID``) prevents concurrent drains from
    double-publishing. Concurrent invocations log and return
    ``{"published": 0, "failed": 0, "skipped": True}``.
    """
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
                return {"published": 0, "failed": 0, "skipped": True}
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
        await publisher.aclose()
    logger.info("drain_changes_outbox finished", extra={"published": published, "failed": failed})
    return {"published": published, "failed": failed}
