"""Pydantic schemas for Watch CRUD operations.

#185 Phase A step 6: per-Watch tracking columns dropped. WatchResponse now
exposes only the stable per-Watch identity and lifecycle fields. Health,
timestamps, and URL live on WatchedItem.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import ULIDStr
from src.core.models.watch import ContentType


class WatchCreate(BaseModel):
    """Schema for creating a new Watch.

    Targets the InfoItem's primary content only. Sub_aspect targeting was
    removed in Archiver v4.0.0.
    """

    name: str = Field(min_length=1, max_length=255)
    info_item_id: ULIDStr
    content_type: ContentType | None = None
    description: str | None = None
    tags: list[str] | None = None


class WatchUpdate(BaseModel):
    """Schema for updating a Watch. All fields optional.

    Identity fields (info_item_id) are immutable after creation — re-target
    by deleting and recreating the Watch.
    """

    name: str | None = None
    content_type: ContentType | None = None
    is_active: bool | None = None
    description: str | None = None
    tags: list[str] | None = None


class WatchResponse(BaseModel):
    """Schema for returning a Watch.

    Per-Watch fields: identity (id, watched_item_id), display
    (name, content_type, description, tags), lifecycle flags
    (is_active, is_archived, suspended_by_domain), timestamps
    (created_at, updated_at). Health and URL live on WatchedItem.
    """

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    watched_item_id: ULIDStr
    content_type: ContentType | None = None
    is_active: bool
    is_archived: bool
    suspended_by_domain: bool
    description: str | None = None
    tags: list[str] | None = None
    created_at: datetime
    updated_at: datetime
