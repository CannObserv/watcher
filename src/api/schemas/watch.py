"""Pydantic schemas for Watch CRUD operations."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import ULIDStr
from src.core.models.watch import ContentType


class WatchCreate(BaseModel):
    """Schema for creating a new watch."""

    name: str
    url: str
    content_type: ContentType
    fetch_config: dict = Field(default_factory=dict)
    schedule_config: dict = Field(default_factory=dict)


class WatchUpdate(BaseModel):
    """Schema for updating a watch. All fields optional."""

    name: str | None = None
    url: str | None = None
    content_type: ContentType | None = None
    fetch_config: dict | None = None
    schedule_config: dict | None = None
    is_active: bool | None = None


class WatchResponse(BaseModel):
    """Schema for returning a watch."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    url: str
    content_type: ContentType
    fetch_config: dict
    schedule_config: dict
    is_active: bool
    created_at: datetime
    updated_at: datetime
