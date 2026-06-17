"""Periodic Procrastinate task draining pending_archiver_sync to Archiver."""

from ulid import ULID

from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.models.change_revision import ChangeRevision
from src.core.models.watched_item import WatchedItem
from src.core.registry import get_registry
from src.core.sources.outbox import delete_pending, mark_failure, select_due
from src.workers import bp

logger = get_logger(__name__)


def _get_archiver_client():
    """Return the ArchiverClient from the process-level registry."""
    return get_registry().get_archiver_client()


@bp.periodic(cron="* * * * *", periodic_id="drain_pending_archiver_sync")
@bp.task(name="drain_pending_archiver_sync", queue="default")
async def drain_pending_archiver_sync(*, batch_size: int = 100, **periodic_kwargs) -> dict:
    """Drain due pending_archiver_sync rows: POST to Archiver, back-populate revision ID."""
    drained = 0
    failed = 0
    session_factory = get_session_factory()
    client = _get_archiver_client()

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

            wi = await session.get(WatchedItem, row.watched_item_id)
            if wi is None or not wi.archiver_info_source_id:
                logger.error(
                    "drain: WatchedItem missing or no archiver_info_source_id, dropping row",
                    extra={
                        "pending_id": str(row.id),
                        "watched_item_id": str(row.watched_item_id),
                    },
                )
                await delete_pending(session, row.id)
                continue

            try:
                out = await client.post_source_revision(
                    info_source_id=str(wi.archiver_info_source_id),
                    content_fingerprint=rev.content_fingerprint,
                    captured_at=rev.captured_at,
                    source_revision_id=str(rev.id),
                    content_cache_uri=row.content_cache_uri,
                    content_cache_expires_at=row.content_cache_expires_at,
                    content_size_bytes=rev.content_size_bytes,
                )
            except Exception as e:
                await mark_failure(session, row, error=f"{type(e).__name__}: {e}")
                failed += 1
                logger.warning(
                    "drain attempt failed",
                    extra={"id": str(row.id), "attempts": row.attempts, "error": str(e)},
                )
                continue

            # Archiver may mint an id differing from the client-supplied one
            # (idempotency on (source, fingerprint)); store the server's. Coerce
            # to ULID to match the Mapped[ULID] column the sweeper keys on (#194).
            # A malformed id is a server-contract violation — isolate it to this
            # row via the failure path rather than aborting the whole batch.
            try:
                archiver_revision_id = ULID.from_str(out.source_revision_id)
            except ValueError as e:
                await mark_failure(session, row, error=f"malformed source_revision_id: {e}")
                failed += 1
                logger.warning(
                    "drain: malformed archiver source_revision_id",
                    extra={
                        "id": str(row.id),
                        "source_revision_id": str(out.source_revision_id),
                        "error": str(e),
                    },
                )
                continue

            rev.archiver_revision_id = archiver_revision_id
            await delete_pending(session, row.id)
            drained += 1

        await session.commit()

    logger.info(
        "drain_pending_archiver_sync finished",
        extra={"drained": drained, "failed": failed},
    )
    return {"drained": drained, "failed": failed}
