"""Pydantic schemas for notification config CRUD (Apprise v2)."""

from datetime import datetime

import apprise
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.schemas.content_config import ContentConfig
from src.api.schemas.types import ULIDStr
from src.core.notifications.apprise_builder import assemble_url
from src.core.notifications.events import WatchEventType

_VALID_EVENT_TYPES = {e.value for e in WatchEventType}


def validate_event_list(events: list[str]) -> list[str]:
    """Raise ValueError if events is empty or contains unknown WatchEventType values."""
    if not events:
        raise ValueError("At least one event must be selected.")
    invalid = [e for e in events if e not in _VALID_EVENT_TYPES]
    if invalid:
        raise ValueError(
            f"Unknown event type(s): {invalid}. Valid types: {sorted(_VALID_EVENT_TYPES)}"
        )
    return events


def validate_apprise_url(url: str) -> str:
    """Reject URLs that Apprise cannot parse."""
    ap = apprise.Apprise()
    if not ap.add(url):
        raise ValueError(
            f"Invalid Apprise URL: {url!r}. "
            "See https://github.com/caronc/apprise/wiki for valid URL formats."
        )
    return url


def extract_channel_hint(url: str) -> str:
    """Return the URL scheme portion (e.g. 'slack' from 'slack://...')."""
    return url.split("://")[0].lower() if "://" in url else url.lower()


class WatchNotificationConfigCreate(BaseModel):
    """
    Request body for creating a notification config.

    Accepts either:
    - apprise_url (raw Apprise URL string), or
    - schema + tokens (assembled server-side into an Apprise URL).
    """

    apprise_url: str | None = None
    plugin_schema: str | None = None
    tokens: dict[str, str] | None = None
    title: str | None = Field(default=None, max_length=100)
    events: list[str] = Field(default_factory=lambda: ["change_detected"])
    content_config: ContentConfig | None = None

    @model_validator(mode="after")
    def resolve_apprise_url(self) -> "WatchNotificationConfigCreate":
        if self.apprise_url is not None:
            # Raw URL path — validate it
            validate_apprise_url(self.apprise_url)
        elif self.plugin_schema is not None:
            # Token path — assemble the URL
            try:
                self.apprise_url = assemble_url(self.plugin_schema, self.tokens or {})
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            raise ValueError("Provide either 'apprise_url' or 'plugin_schema' + 'tokens'.")
        return self

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class WatchNotificationConfigUpdate(BaseModel):
    """Request body for PATCH — all fields optional."""

    is_active: bool | None = None
    events: list[str] | None = None
    apprise_url: str | None = None
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

    @field_validator("apprise_url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_apprise_url(v)


class WatchNotificationConfigResponse(BaseModel):
    """Response schema — never exposes apprise_url."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watch_id: ULIDStr
    title: str | None
    channel_hint: str
    events: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    content_config: ContentConfig | None = None

    @field_validator("content_config", mode="before")
    @classmethod
    def parse_content_config(cls, v: dict | None) -> ContentConfig | None:
        if v is None:
            return None
        return ContentConfig.model_validate(v)
