"""Pydantic schemas for Watch CRUD operations.

Phase 5 contract: ``url`` and ``fetch_config`` no longer live on the Watch
model — they are owned by the canonical InfoSource and resolved at runtime via
the ArchiverClient SDK. ``WatchCreate`` accepts ``info_source_id`` (required)
and derives URL from it. ``WatchResponse`` exposes neither legacy URL field.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import HttpUrlStr, ULIDStr
from src.core.models.watch import ContentType, WatchHealthStatus


class WatchCreate(BaseModel):
    """Schema for creating a new watch.

    The watch is bound to a pre-existing InfoSource in the Archiver service.
    ``info_source_id`` (required) identifies the root or fragment InfoSource;
    the route resolves the target URL from it and enforces the fragment-root
    invariant.
    """

    name: str = Field(min_length=1, max_length=255)
    info_source_id: ULIDStr
    content_type: ContentType
    description: str | None = None
    tags: list[str] | None = None
    schedule_config: dict = Field(default_factory=dict)


class WatchUpdate(BaseModel):
    """Schema for updating a watch. All fields optional.

    ``url`` / ``fetch_config`` no longer exist on the model.
    """

    name: str | None = None
    content_type: ContentType | None = None
    schedule_config: dict | None = None
    is_active: bool | None = None
    effective_url: HttpUrlStr | None = None
    effective_domain: str | None = Field(default=None, max_length=253)
    description: str | None = None
    tags: list[str] | None = None


class WatchResponse(BaseModel):
    """Schema for returning a watch.

    Does not include ``url`` or ``fetch_config`` — those are properties of
    the watch's InfoSpec, fetched from the Information service.
    """

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    info_source_id: ULIDStr
    content_type: ContentType
    schedule_config: dict
    is_active: bool
    is_archived: bool
    domain_suspended: bool
    last_checked_at: datetime | None = None
    last_changed_at: datetime | None = None
    effective_url: str | None = None
    effective_domain: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    health_status: WatchHealthStatus
    created_at: datetime
    updated_at: datetime
