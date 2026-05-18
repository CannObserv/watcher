"""Pydantic schemas for WatchedItem and WatchedItemNotificationTemplate API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.types import ULIDStr
from src.api.schemas.validators import validate_event_list
from src.core.models.watch import ContentType


class WatchedItemCreate(BaseModel):
    """Create a standalone WatchedItem.

    Identity is ``info_item_id`` (must reference a known Archiver InfoItem,
    1:1). The InfoItem's existence is NOT validated here — the route layer
    does that via the Archiver SDK and maps NotFound → 422.
    """

    info_item_id: ULIDStr
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    default_schedule_config: dict | None = None
    default_content_type: str | None = None
    default_tags: list[str] | None = None

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
    """Partial update to a WatchedItem. All fields optional."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    default_schedule_config: dict | None = None
    default_content_type: str | None = None
    default_tags: list[str] | None = None

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
    """Single WatchedItem record."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    info_item_id: ULIDStr
    name: str
    description: str | None
    is_active: bool
    archived_at: datetime | None
    last_reviewed_at: datetime | None
    default_schedule_config: dict | None
    default_content_type: str | None
    default_tags: list[str] | None
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
