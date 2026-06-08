"""Pydantic schemas for Watch CRUD operations.

#185 Phase A: Watch is InfoItem-first. ``info_item_id`` is required; the
sub_aspect concept (``target_info_source_id``) was removed in Archiver v4.0.0.
Schedule + URL no longer live on the Watch — scheduling is owned by the parent
WatchedItem; URL is resolved from the InfoItem's primary InfoSource via the
ArchiverClient at create-time and stored as ``effective_url`` on both Watch and
WatchedItem.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import HttpUrlStr, ULIDStr
from src.core.models.watch import ContentType, WatchHealthStatus


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

    ``info_item_id`` and ``target_info_source_id`` are immutable after
    creation — re-target by deleting and recreating the Watch.
    """

    name: str | None = None
    content_type: ContentType | None = None
    is_active: bool | None = None
    effective_url: HttpUrlStr | None = None
    description: str | None = None
    tags: list[str] | None = None


class WatchResponse(BaseModel):
    """Schema for returning a Watch.

    Exposes the identity columns (``info_item_id``, ``target_info_source_id``,
    ``watched_item_id``) and the cached ``effective_url`` snapshot.
    Domain info lives on WatchedItem.
    """

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    info_item_id: ULIDStr
    target_info_source_id: ULIDStr | None = None
    watched_item_id: ULIDStr
    content_type: ContentType | None = None
    is_active: bool
    is_archived: bool
    domain_suspended: bool
    last_checked_at: datetime | None = None
    last_changed_at: datetime | None = None
    effective_url: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    health_status: WatchHealthStatus
    created_at: datetime
    updated_at: datetime
