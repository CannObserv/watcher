"""Pydantic schemas for Domain API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.types import ULIDStr
from src.core.scheduler import validate_optional_schedule_config


class DomainPatch(BaseModel):
    """Schema for creating or updating a domain config (upsert via PATCH)."""

    min_interval: float | None = Field(None, ge=0)
    max_concurrency: int | None = Field(None, ge=1)
    decay_window: float | None = Field(None, ge=1)
    notes: str | None = None
    # Operator's desired check cadence for items on the domain (#205); the Domain
    # tier of schedule resolution. Distinct from min_interval (rate-limiter floor).
    default_schedule_config: dict | None = None

    @field_validator("default_schedule_config")
    @classmethod
    def _cadence(cls, v: dict | None) -> dict | None:
        """Reject an empty/intervalless or malformed cadence at the write boundary."""
        try:
            return validate_optional_schedule_config(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


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
    notes: str | None
    default_schedule_config: dict | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
