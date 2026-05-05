"""Substring search across InfoItem name + description.

v1 uses ``ILIKE '%q%'`` over both fields. Upgrade to ``pg_trgm`` if dataset
size or false-positive rate justifies it (see Phase 3a plan, Open follow-ups).
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.core.models import InfoItem


async def find_info_item(session: AsyncSession, query: str, *, limit: int = 20) -> list[InfoItem]:
    """Return InfoItems whose name or description contains ``query`` (case-insensitive).

    Ordered by ``created_at DESC`` so the newest matches surface first. Empty
    ``query`` raises ``ValueError`` — the route layer should validate before
    calling so the API contract returns 422 on empty/missing input.
    """
    if not query:
        raise ValueError("query must be non-empty")
    pattern = f"%{query}%"
    stmt = (
        select(InfoItem)
        .where(
            or_(
                InfoItem.name.ilike(pattern),
                InfoItem.description.ilike(pattern),
            )
        )
        .order_by(InfoItem.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
