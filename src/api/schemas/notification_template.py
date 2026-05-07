"""Pydantic schemas for NotificationTemplate API (remote-channel only).

After Phase 5 (#137), notification templates are remote-channel pointers
with rendering options; the notifier service owns the actual delivery target.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.api.schemas.content_config import ContentConfig
from src.api.schemas.notification_config import validate_event_list
from src.api.schemas.types import ULIDStr


class NotificationTemplateCreate(BaseModel):
    title: str = Field(..., max_length=100)
    remote_channel_id: str = Field(..., min_length=26, max_length=26)
    channel_hint: str | None = Field(default=None, max_length=50)
    events: list[str] = ["change_detected"]
    is_global_default: bool = False
    content_config: ContentConfig | None = None

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class NotificationTemplateUpdate(BaseModel):
    title: str | None = Field(None, max_length=100)
    remote_channel_id: str | None = Field(default=None, min_length=26, max_length=26)
    channel_hint: str | None = Field(default=None, max_length=50)
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
