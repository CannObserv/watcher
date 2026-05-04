"""Drain unpublished Changes from the outbox to the Redis bus.

Phase 2b ships a generic envelope built from existing Change row fields.
Phase 2c will refine the payload with info_item_id, info_spec_id, fingerprints.
"""

import json

from src.core.changes.outbox import mark_published, select_unpublished
from src.core.changes.publisher import ChangePublisher
from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.utils import format_utc_iso
from src.workers import bp

logger = get_logger(__name__)

INFO_CHANGES_TOPIC = "info.changes"


def _build_envelope(change) -> bytes:
    """Build the JSON wire envelope for a Change row.

    Phase 2b shape (generic):
      {
        "change_id": "<ULID>",
        "watch_id": "<ULID>",
        "previous_snapshot_id": "<ULID>",
        "current_snapshot_id": "<ULID>",
        "detected_at": "<ISO8601 UTC>",
        "significance": <float | null>,
        "visual_change_score": <float | null>,
        "metadata": <dict>
      }
    Phase 2c will add info_item_id and info_spec_id.
    """
    return json.dumps(
        {
            "change_id": str(change.id),
            "watch_id": str(change.watch_id),
            "previous_snapshot_id": str(change.previous_snapshot_id),
            "current_snapshot_id": str(change.current_snapshot_id),
            "detected_at": format_utc_iso(change.detected_at),
            "significance": change.significance,
            "visual_change_score": change.visual_change_score,
            "metadata": change.change_metadata,
        }
    ).encode("utf-8")


@bp.task(name="drain_changes_outbox", queue="default")
async def drain_changes_outbox(*, batch_size: int = 100) -> dict:
    """Publish up to ``batch_size`` unpublished Changes; return counts.

    Idempotent — only processes rows where ``published_to_bus_at IS NULL``.
    Per-row errors are caught and counted in ``failed``; the rest of the batch
    continues. Failed rows remain unpublished and the next drain picks them up.
    """
    publisher = ChangePublisher()
    published = 0
    failed = 0
    try:
        async with get_session_factory()() as session:
            rows = await select_unpublished(session, limit=batch_size)
            for change in rows:
                try:
                    payload = _build_envelope(change)
                    msg_id = await publisher.publish_change(
                        topic=INFO_CHANGES_TOPIC,
                        # Phase 2b uses watch_id as partition key; 2c switches to info_item_id.
                        key=str(change.watch_id),
                        payload=payload,
                        headers={"schema_version": "1"},
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
