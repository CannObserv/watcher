"""Pydantic schema for AuditLog responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.api.schemas.types import ULIDStr


class AuditLogResponse(BaseModel):
    """Response schema for an audit log entry."""

    model_config = ConfigDict(from_attributes=True)
    id: ULIDStr
    event_type: str
    watch_id: ULIDStr | None
    payload: dict
    created_at: datetime
