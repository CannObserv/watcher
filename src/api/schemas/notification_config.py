"""Pydantic schemas for notification config CRUD (remote-channel only).

After Phase 5 (#137), notification configs are pure remote-channel pointers:
the notifier service owns the actual delivery target. No Apprise URL is
accepted, validated, or stored here.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.content_config import ContentConfig
from src.api.schemas.types import ULIDStr
from src.api.schemas.validators import validate_event_list


class WatchNotificationConfigCreate(BaseModel):
    """Request body for creating a notification config.

    `remote_channel_id` is the notifier-service channel ULID; required.
    `str_strip_whitespace` runs before length validation, so a
    whitespace-only `channel_hint` collapses to ``""`` and trips
    `min_length=1`.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    remote_channel_id: str = Field(..., min_length=26, max_length=26)
    channel_hint: str = Field(default="remote", min_length=1, max_length=50)
    title: str | None = Field(default=None, max_length=100)
    events: list[str] = Field(default_factory=lambda: ["change_detected"])
    content_config: ContentConfig | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class WatchNotificationConfigUpdate(BaseModel):
    """Request body for PATCH — all fields optional.

    `channel_hint` stays nullable on Update so the route can use
    ``model_fields_set`` to distinguish "not provided" (no-op) from a
    user-supplied value. Same pattern as ``title``. The Create schema
    is `str` (always present, default `"remote"`) — the asymmetry is
    intentional.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    is_active: bool | None = None
    events: list[str] | None = None
    remote_channel_id: str | None = Field(default=None, min_length=26, max_length=26)
    channel_hint: str | None = Field(default=None, min_length=1, max_length=50)
    # title uses model_fields_set in the route to distinguish "omitted" (no-op)
    # from "explicitly set to null" (clears the title). Default None means an
    # absent key won't end up in model_fields_set, so skipping the field is safe.
    title: str | None = Field(default=None, max_length=100)
    content_config: ContentConfig | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return validate_event_list(v)


class WatchNotificationConfigResponse(BaseModel):
    """Response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watched_item_id: ULIDStr
    title: str | None
    channel_hint: str
    events: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    content_config: ContentConfig | None = None
    remote_channel_id: str | None = None

    @field_validator("content_config", mode="before")
    @classmethod
    def parse_content_config(cls, v: dict | None) -> ContentConfig | None:
        if v is None:
            return None
        return ContentConfig.model_validate(v)
