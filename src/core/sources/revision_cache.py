"""Read/write the Watcher-local last_known_revisions table."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.last_known_revision import LastKnownRevision


async def get_last_fingerprint(
    session: AsyncSession,
    info_source_id: str,
) -> str | None:
    """Return the cached fingerprint for `info_source_id`, or None."""
    result = await session.execute(
        select(LastKnownRevision.content_fingerprint).where(
            LastKnownRevision.info_source_id == info_source_id
        )
    )
    return result.scalar_one_or_none()


async def upsert_last_known(
    session: AsyncSession,
    *,
    info_source_id: str,
    content_fingerprint: str,
    source_revision_id: str,
    captured_at: datetime,
) -> None:
    """Upsert the cache row for `info_source_id` (PK-keyed)."""
    stmt = (
        pg_insert(LastKnownRevision)
        .values(
            info_source_id=info_source_id,
            content_fingerprint=content_fingerprint,
            source_revision_id=source_revision_id,
            captured_at=captured_at,
        )
        .on_conflict_do_update(
            index_elements=["info_source_id"],
            set_={
                "content_fingerprint": content_fingerprint,
                "source_revision_id": source_revision_id,
                "captured_at": captured_at,
            },
        )
    )
    await session.execute(stmt)
    await session.flush()
