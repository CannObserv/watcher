"""Pydantic schemas for NotificationTemplate API (remote-channel only).

After Phase 5 (#137), notification templates are remote-channel pointers
with rendering options; the notifier service owns the actual delivery target.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.content_config import ContentConfig
from src.api.schemas.types import ULIDStr
from src.api.schemas.validators import validate_event_list


class NotificationTemplateCreate(BaseModel):
    """`str_strip_whitespace` runs before length validation, so a
    whitespace-only `channel_hint` collapses to ``""`` and trips
    `min_length=1`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., max_length=100)
    remote_channel_id: str = Field(..., min_length=26, max_length=26)
    channel_hint: str = Field(default="remote", min_length=1, max_length=50)
    events: list[str] = ["change_detected"]
    is_global_default: bool = False
    content_config: ContentConfig | None = None

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class NotificationTemplateUpdate(BaseModel):
    """`channel_hint` stays nullable on Update so the route can use
    ``model_fields_set`` to distinguish "not provided" (no-op) from a
    user-supplied value. Same pattern as ``title``. The Create schema
    is `str` (always present, default `"remote"`) — the asymmetry is
    intentional."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(None, max_length=100)
    remote_channel_id: str | None = Field(default=None, min_length=26, max_length=26)
    channel_hint: str | None = Field(default=None, min_length=1, max_length=50)
    events: list[str] | None = None
    is_global_default: bool | None = None
    is_active: bool | None = None
    content_config: ContentConfig | None = None

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            return validate_event_list(v)
        return v


class NotificationTemplateResponse(BaseModel):
    id: ULIDStr
    title: str
    channel_hint: str
    events: list[str]
    is_global_default: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    watch_ref_count: int = 0
    domain_ref_count: int = 0
    content_config: ContentConfig | None = None
    remote_channel_id: str | None = None

    model_config = {"from_attributes": True}

    @field_validator("content_config", mode="before")
    @classmethod
    def parse_content_config(cls, v: dict | None) -> ContentConfig | None:
        if v is None:
            return None
        return ContentConfig.model_validate(v)
