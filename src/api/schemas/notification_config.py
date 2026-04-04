"""Pydantic schemas for notification config CRUD (Apprise v2)."""

from datetime import datetime
from typing import Annotated

import apprise
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.types import ULIDStr
from src.core.notifications.events import WatchEventType

_VALID_EVENT_TYPES = {e.value for e in WatchEventType}


def _validate_apprise_url(url: str) -> str:
    """Reject URLs that Apprise cannot parse."""
    ap = apprise.Apprise()
    if not ap.add(url):
        raise ValueError(
            f"Invalid Apprise URL: {url!r}. "
            "See https://github.com/caronc/apprise/wiki for valid URL formats."
        )
    return url


def _extract_channel_hint(url: str) -> str:
    """Return the URL scheme portion (e.g. 'slack' from 'slack://...')."""
    return url.split("://")[0].lower() if "://" in url else url.lower()


class NotificationConfigCreate(BaseModel):
    """Request body for creating a notification config."""

    apprise_url: Annotated[str, Field(min_length=1)]
    events: list[str] = Field(default_factory=lambda: ["change_detected"])

    @field_validator("apprise_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _validate_apprise_url(v)

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        invalid = [e for e in v if e not in _VALID_EVENT_TYPES]
        if invalid:
            raise ValueError(
                f"Unknown event type(s): {invalid}. Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )
        return v


class NotificationConfigUpdate(BaseModel):
    """Request body for PATCH — all fields optional."""

    is_active: bool | None = None
    events: list[str] | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        invalid = [e for e in v if e not in _VALID_EVENT_TYPES]
        if invalid:
            raise ValueError(
                f"Unknown event type(s): {invalid}. Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )
        return v


class NotificationConfigResponse(BaseModel):
    """Response schema — never exposes apprise_url."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watch_id: ULIDStr
    channel_hint: str
    events: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
