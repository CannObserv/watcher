"""Pydantic schemas for WatchedItem, WatchedItemNotificationTemplate, and ChangeRevision."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.schemas.types import HttpUrlStr, ULIDStr
from src.api.schemas.validators import validate_event_list
from src.core.models.watched_item import ContentType, WatchHealthStatus


class WatchedItemCreate(BaseModel):
    """Create a WatchedItem via ``POST /api/v1/watched-items``.

    Two creation paths:
    - **InfoItem-linked** (``archiver_info_item_id`` provided): the InfoItem's existence
      is validated via the Archiver SDK (NotFound → 422); name defaults to the
      InfoItem's name when omitted.
    - **URL-only** (``url`` provided, no ``archiver_info_item_id``): the URL is probed
      for ``effective_url`` + ``domain_name``; name defaults to the probed
      domain. Produces a WatchedItem with ``archiver_info_item_id=None`` (#185 Phase A).

    At least one of ``archiver_info_item_id`` or ``url`` is required.

    ``source_specs`` seeds the local pipeline extraction config. Optional at
    create time; updatable later via PATCH.
    """

    archiver_info_item_id: ULIDStr | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    default_schedule_config: dict | None = None
    default_content_type: str | None = None
    default_tags: list[str] | None = None
    url: HttpUrlStr | None = None
    source_specs: list[dict] | None = None
    archiver_info_source_id: str | None = Field(None, min_length=1, max_length=26)

    @model_validator(mode="after")
    def _require_anchor(self) -> "WatchedItemCreate":
        if not self.archiver_info_item_id and not self.url:
            raise ValueError("At least one of archiver_info_item_id or url is required")
        return self

    @field_validator("default_content_type")
    @classmethod
    def _ct(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            ContentType(v)
        except ValueError as exc:
            raise ValueError(f"Invalid default_content_type: {v!r}") from exc
        return v


class WatchedItemPatch(BaseModel):
    """Partial update to a WatchedItem. All fields optional.

    ``effective_url`` is set directly without re-probing — Archiver is the
    authoritative source for URL succession.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    default_schedule_config: dict | None = None
    default_content_type: str | None = None
    default_tags: list[str] | None = None
    effective_url: HttpUrlStr | None = None
    source_specs: list[dict] | None = None
    archiver_info_source_id: str | None = Field(None, min_length=1, max_length=26)

    @model_validator(mode="after")
    def _reject_explicit_null(self) -> "WatchedItemPatch":
        """Reject explicit null for NOT NULL DB columns; omitting the field is fine."""
        for field in ("name", "is_active", "effective_url", "source_specs"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit the field to leave it unchanged")
        return self

    @field_validator("default_content_type")
    @classmethod
    def _ct(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            ContentType(v)
        except ValueError as exc:
            raise ValueError(f"Invalid default_content_type: {v!r}") from exc
        return v


class WatchedItemResponse(BaseModel):
    """Single WatchedItem record.

    ``archiver_info_item_id`` is null for WatchedItems created via the dashboard
    (URL-first, no InfoItem required). API-created WatchedItems always have it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    archiver_info_item_id: ULIDStr | None = None
    name: str
    description: str | None
    is_active: bool
    archived_at: datetime | None
    last_reviewed_at: datetime | None
    last_checked_at: datetime | None
    last_changed_at: datetime | None
    health_status: WatchHealthStatus
    default_schedule_config: dict | None
    default_content_type: str | None
    default_tags: list[str] | None
    effective_url: str
    source_specs: list[dict]
    archiver_info_source_id: str | None = None
    domain_name: str | None = None
    domain_suspended: bool = False
    created_at: datetime
    updated_at: datetime


class WatchedItemTemplateCreate(BaseModel):
    """Create a notification template under a WatchedItem."""

    title: str | None = Field(None, max_length=100)
    channel_hint: str = Field(..., min_length=1, max_length=50)
    events: list[str] = Field(default_factory=lambda: ["change_detected"])
    is_active: bool = True
    content_config: dict | None = None
    remote_channel_id: str | None = Field(None, max_length=26)

    @field_validator("events")
    @classmethod
    def _ev(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class WatchedItemTemplatePatch(BaseModel):
    """Partial update to a WatchedItemNotificationTemplate."""

    title: str | None = Field(None, max_length=100)
    channel_hint: str | None = Field(None, min_length=1, max_length=50)
    events: list[str] | None = None
    is_active: bool | None = None
    content_config: dict | None = None
    remote_channel_id: str | None = Field(None, max_length=26)

    @field_validator("events")
    @classmethod
    def _ev(cls, v: list[str] | None) -> list[str] | None:
        return validate_event_list(v) if v is not None else None


class WatchedItemTemplateResponse(BaseModel):
    """Single notification template under a WatchedItem."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watched_item_id: ULIDStr
    title: str | None
    channel_hint: str
    events: list[str]
    is_active: bool
    content_config: dict | None
    remote_channel_id: str | None
    created_at: datetime
    updated_at: datetime


class ChangeRevisionResponse(BaseModel):
    """One ChangeRevision record for a WatchedItem."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watched_item_id: ULIDStr
    content_fingerprint: str
    captured_at: datetime
    content_size_bytes: int | None
    archiver_revision_id: ULIDStr | None
    schema_version: int
