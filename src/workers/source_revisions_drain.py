"""Periodic Procrastinate task draining pending_source_revisions to Archiver."""

from datetime import UTC, datetime

from sqlalchemy import select

from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.watch import Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.registry import get_registry
from src.core.sources.outbox import delete_pending, mark_failure, select_due
from src.core.sources.revision_cache import upsert_last_known
from src.workers import bp

logger = get_logger(__name__)


def _get_archiver_client():
    """Return the ArchiverClient from the process-level registry."""
    return get_registry().get_archiver_client()


async def _resolve_watch_for_source(session, info_source_id: str) -> Watch | None:
    """Return the active Watch for this source, or None.

    Returns None when the Watch model does not yet have an ``info_source_id``
    column (pre-Stage-6 migration) — the caller falls back to a source-only
    event in that case.
    """
    if not hasattr(Watch, "info_source_id"):
        return None
    result = await session.execute(
        select(Watch)
        .where(Watch.info_source_id == info_source_id)
        .where(Watch.is_active.is_(True))
        .where(Watch.is_archived.is_(False))
    )
    return result.scalar_one_or_none()


@bp.periodic(cron="* * * * *", periodic_id="drain_pending_source_revisions")
@bp.task(name="drain_pending_source_revisions", queue="default")
async def drain_pending_source_revisions(*, batch_size: int = 100, **periodic_kwargs) -> dict:
    """Drain due outbox rows: POST each to Archiver, dispatch on success, mark_failure on error."""
    drained = 0
    failed = 0
    session_factory = get_session_factory()
    client = _get_archiver_client()

    async with session_factory() as session:
        rows = await select_due(session, limit=batch_size)
        for row in rows:
            try:
                out = await client.post_source_revision(
                    info_source_id=str(row.info_source_id),
                    content_fingerprint=row.content_fingerprint,
                    captured_at=row.captured_at,
                    source_revision_id=str(row.id),
                    content_cache_uri=row.content_cache_uri,
                    content_cache_expires_at=row.content_cache_expires_at,
                    content_size_bytes=row.content_size_bytes,
                    content_media_type=row.content_media_type,
                )
            except Exception as e:
                await mark_failure(session, row, error=f"{type(e).__name__}: {e}")
                failed += 1
                logger.warning(
                    "drain attempt failed",
                    extra={"id": str(row.id), "attempts": row.attempts, "error": str(e)},
                )
                continue

            canonical_id = str(out.source_revision_id)

            await upsert_last_known(
                session,
                info_source_id=str(row.info_source_id),
                content_fingerprint=row.content_fingerprint,
                source_revision_id=canonical_id,
                captured_at=row.captured_at,
            )

            watch = await _resolve_watch_for_source(session, str(row.info_source_id))
            event = WatchEvent(
                event_type=WatchEventType.CHANGE_DETECTED,
                watch_id=str(watch.id) if watch is not None else "",
                watch_name=watch.name if watch is not None else "",
                watch_url=(watch.effective_url or "") if watch is not None else "",
                occurred_at=datetime.now(UTC),
                metadata={
                    "source_revision_id": canonical_id,
                    "info_source_id": str(row.info_source_id),
                    "content_fingerprint": row.content_fingerprint,
                    "deferred": True,
                },
            )
            await dispatch_event_notifications(session, event)

            await delete_pending(session, row.id)
            drained += 1

        await session.commit()

    logger.info(
        "drain_pending_source_revisions finished",
        extra={"drained": drained, "failed": failed},
    )
    return {"drained": drained, "failed": failed}
