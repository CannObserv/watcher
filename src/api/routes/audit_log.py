"""Audit log API endpoints — query system event history."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.audit_log import AuditLogResponse
from src.core.models.audit_log import AuditLog

router = APIRouter(prefix="/api/audit", tags=["audit-log"])


@router.get("", response_model=list[AuditLogResponse])
async def list_audit_entries(
    event_type: str | None = Query(None),
    watch_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    """List audit log entries with optional filters and pagination."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if watch_id:
        try:
            parsed = ULID.from_str(watch_id)
        except ValueError:
            return []
        stmt = stmt.where(AuditLog.watch_id == parsed)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()
