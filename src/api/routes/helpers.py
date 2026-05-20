"""Shared route helpers — ULID parsing, common lookups."""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.watch import Watch


def parse_ulid(value: str, label: str = "Resource") -> ULID:
    """Parse a ULID string, raising 404 on invalid format."""
    try:
        return ULID.from_str(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"{label} not found") from exc


def parse_filter_ulid(value: str, field: str = "id") -> ULID:
    """Parse a ULID query-parameter filter value, raising 400 on invalid format.

    Use for filter params on list endpoints (not path parameters — use parse_ulid there).
    """
    try:
        return ULID.from_str(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field} format") from exc


async def get_watch_or_404(watch_id: str, session: AsyncSession) -> Watch:
    """Fetch a watch by ID string, raising 404 if not found."""
    watch = await session.get(Watch, parse_ulid(watch_id, "Watch"))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    return watch
