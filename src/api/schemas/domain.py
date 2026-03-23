"""Pydantic schemas for Domain API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import ULIDStr


class DomainPatch(BaseModel):
    """Schema for creating or updating a domain config (upsert via PATCH)."""

    min_interval: float | None = Field(None, ge=0)
    max_concurrency: int | None = Field(None, ge=1)
    decay_window: float | None = Field(None, ge=0)


class DomainResponse(BaseModel):
    """Schema for returning a domain config."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    min_interval: float
    max_concurrency: int
    current_interval: float
    last_request_at: datetime | None
    decay_window: float
    created_at: datetime
    updated_at: datetime
