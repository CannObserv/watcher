"""Pydantic schemas for the NotificationTemplate API (#200, remote-channel only).

Post-#200 a NotificationTemplate carries an intrinsic ``visibility`` (global /
domain / watched_item); there is no separate "config" object. After Phase 5
(#137) templates are remote-channel pointers with rendering options; the
notifier service owns the actual delivery target.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.schemas.content_config import ContentConfig
from src.api.schemas.types import ULIDStr
from src.api.schemas.validators import validate_event_list
from src.core.models.notification_template import (
    VISIBILITIES,
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
)


class NotificationTemplateCreate(BaseModel):
    """Create a notification template at any visibility scope.

    ``str_strip_whitespace`` runs before length validation, so a whitespace-only
    ``channel_hint`` collapses to ``""`` and trips ``min_length=1``. The
    ``visibility``/ref consistency rule mirrors the DB CHECK constraint.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., max_length=100)
    remote_channel_id: str = Field(..., min_length=26, max_length=26)
    channel_hint: str = Field(default="remote", min_length=1, max_length=50)
    events: list[str] = Field(default_factory=lambda: ["change_detected"])
    visibility: str = VISIBILITY_GLOBAL
    domain_name: str | None = Field(default=None, max_length=253)
    watched_item_id: ULIDStr | None = None
    content_config: ContentConfig | None = None

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)

    @field_validator("visibility")
    @classmethod
    def check_visibility(cls, v: str) -> str:
        if v not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {VISIBILITIES}")
        return v

    @model_validator(mode="after")
    def check_scope_refs(self) -> NotificationTemplateCreate:
        """Exactly the ref column implied by ``visibility`` must be present."""
        if self.visibility == VISIBILITY_GLOBAL and (self.domain_name or self.watched_item_id):
            raise ValueError("global templates must not set domain_name or watched_item_id")
        if self.visibility == VISIBILITY_DOMAIN and (not self.domain_name or self.watched_item_id):
            raise ValueError("domain templates require domain_name and no watched_item_id")
        if self.visibility == VISIBILITY_WATCHED_ITEM and (
            not self.watched_item_id or self.domain_name
        ):
            raise ValueError("watched_item templates require watched_item_id and no domain_name")
        return self


class NotificationTemplateUpdate(BaseModel):
    """Partial update. ``visibility`` and its refs are intrinsic and not updatable
    here — re-scoping a template means delete + recreate.

    ``channel_hint`` stays nullable on Update so the route can use
    ``model_fields_set`` to distinguish "not provided" (no-op) from a
    user-supplied value. Same pattern as ``title``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(None, max_length=100)
    remote_channel_id: str | None = Field(default=None, min_length=26, max_length=26)
    channel_hint: str | None = Field(default=None, min_length=1, max_length=50)
    events: list[str] | None = None
    is_active: bool | None = None
    content_config: ContentConfig | None = None

    def to_updates(self) -> dict:
        """Translate the PATCH body into a field→value dict for the service (#228).

        ``title``/``channel_hint``/``remote_channel_id`` require a provided,
        non-None value (nullable only so ``model_fields_set`` can distinguish
        "not provided"); ``events``/``is_active`` update on any non-None value;
        ``content_config`` updates whenever provided — ``None`` clears it.
        """
        updates: dict = {}
        for field in ("title", "channel_hint", "remote_channel_id"):
            if field in self.model_fields_set and getattr(self, field) is not None:
                updates[field] = getattr(self, field)
        if self.events is not None:
            updates["events"] = self.events
        if self.is_active is not None:
            updates["is_active"] = self.is_active
        if "content_config" in self.model_fields_set:
            updates["content_config"] = (
                self.content_config.model_dump() if self.content_config else None
            )
        return updates

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            return validate_event_list(v)
        return v


class ItemNotificationTemplateCreate(BaseModel):
    """Create a watched-item-scoped template via the nested item route.

    ``visibility`` and ``watched_item_id`` are pinned by the route path, so the
    body omits them.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., max_length=100)
    remote_channel_id: str = Field(..., min_length=26, max_length=26)
    channel_hint: str = Field(default="remote", min_length=1, max_length=50)
    events: list[str] = Field(default_factory=lambda: ["change_detected"])
    content_config: ContentConfig | None = None

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class NotificationTemplateResponse(BaseModel):
    id: ULIDStr
    title: str
    channel_hint: str
    events: list[str]
    visibility: str
    domain_name: str | None = None
    watched_item_id: ULIDStr | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    content_config: ContentConfig | None = None
    remote_channel_id: str | None = None

    model_config = {"from_attributes": True}

    @field_validator("content_config", mode="before")
    @classmethod
    def parse_content_config(cls, v: dict | None) -> ContentConfig | None:
        if v is None:
            return None
        return ContentConfig.model_validate(v)
