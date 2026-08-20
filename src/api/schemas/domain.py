"""Pydantic schemas for Domain API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.types import ULIDStr
from src.core.scheduling.cadence import validate_optional_schedule_config


class DomainPatch(BaseModel):
    """Schema for creating or updating a domain config (upsert via PATCH)."""

    min_interval: float | None = Field(None, ge=0)
    notes: str | None = None
    # Operator's desired check cadence for items on the domain (#205); the Domain
    # tier of schedule resolution. Distinct from min_interval, the politeness floor
    # published to Replicator (#245).
    default_schedule_config: dict | None = None

    @field_validator("default_schedule_config")
    @classmethod
    def _cadence(cls, v: dict | None) -> dict | None:
        """Reject an empty/intervalless or malformed cadence at the write boundary.

        ``validate_optional_schedule_config`` raises ``ValueError``, which Pydantic
        surfaces as a 422 — no wrapping needed.
        """
        return validate_optional_schedule_config(v)


class DomainResponse(BaseModel):
    """Schema for returning a domain config."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    min_interval: float
    notes: str | None
    default_schedule_config: dict | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
