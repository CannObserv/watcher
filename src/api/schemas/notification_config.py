"""Pydantic schemas for notification config CRUD."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import ULIDStr


class NotificationConfigCreate(BaseModel):
    """Schema for creating a notification config."""

    channel: str
    config: dict = Field(default_factory=dict)


class NotificationConfigResponse(BaseModel):
    """Schema for returning a notification config."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watch_id: ULIDStr
    channel: str
    config: dict
    is_active: bool
    created_at: datetime
    updated_at: datetime
