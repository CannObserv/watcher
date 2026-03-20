"""Pydantic schemas for Watch CRUD operations."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

from src.core.models.watch import ContentType

ULIDStr = Annotated[str, BeforeValidator(lambda v: str(v))]


class WatchCreate(BaseModel):
    """Schema for creating a new watch."""

    name: str
    url: str
    content_type: ContentType
    fetch_config: dict = {}
    schedule_config: dict = {}


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
