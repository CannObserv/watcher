"""Pydantic schemas for Watch CRUD operations."""

import re
from datetime import datetime

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.types import HttpUrlStr, ULIDStr
from src.core.models.watch import ContentType


def _validate_ignore_patterns(fetch_config: dict | None) -> dict | None:
    """Validate that fetch_config['ignore_patterns'] contains valid regexes."""
    if not fetch_config:
        return fetch_config
    patterns = fetch_config.get("ignore_patterns")
    if patterns is None:
        return fetch_config
    if not isinstance(patterns, list):
        raise ValueError("fetch_config.ignore_patterns must be a list of strings")
    for i, p in enumerate(patterns):
        if not isinstance(p, str):
            raise ValueError(f"fetch_config.ignore_patterns[{i}] must be a string")
        try:
            re.compile(p)
        except re.error as exc:
            raise ValueError(
                f"fetch_config.ignore_patterns[{i}] is not a valid regex: {exc}"
            ) from exc
    return fetch_config


def _validate_ignore_selectors(fetch_config: dict | None) -> dict | None:
    """Validate that fetch_config['ignore_selectors'] contains valid CSS selectors."""
    if not fetch_config:
        return fetch_config
    selectors = fetch_config.get("ignore_selectors")
    if selectors is None:
        return fetch_config
    if not isinstance(selectors, list):
        raise ValueError("fetch_config.ignore_selectors must be a list of strings")
    soup = BeautifulSoup("", "lxml")
    for i, s in enumerate(selectors):
        if not isinstance(s, str):
            raise ValueError(f"fetch_config.ignore_selectors[{i}] must be a string")
        try:
            soup.select(s)
        except Exception as exc:
            raise ValueError(
                f"fetch_config.ignore_selectors[{i}] is not a valid CSS selector: {exc}"
            ) from exc
    return fetch_config


def _validate_viewport(fetch_config: dict | None) -> dict | None:
    """Validate fetch_config viewport_width and viewport_height."""
    if not fetch_config:
        return fetch_config
    for key, max_val in (("viewport_width", 7680), ("viewport_height", 4320)):
        val = fetch_config.get(key)
        if val is None:
            continue
        if not isinstance(val, int) or val <= 0 or val > max_val:
            raise ValueError(
                f"fetch_config.{key} must be a positive integer no greater than {max_val}"
            )
    return fetch_config


def _validate_fetch_config(fetch_config: dict | None) -> dict | None:
    """Run all fetch_config validators."""
    fetch_config = _validate_ignore_patterns(fetch_config)
    fetch_config = _validate_ignore_selectors(fetch_config)
    fetch_config = _validate_viewport(fetch_config)
    return fetch_config


class WatchCreate(BaseModel):
    """Schema for creating a new watch."""

    name: str
    url: HttpUrlStr
    content_type: ContentType
    fetch_config: dict = Field(default_factory=dict)
    schedule_config: dict = Field(default_factory=dict)

    @field_validator("fetch_config")
    @classmethod
    def validate_fetch_config(cls, v: dict) -> dict:
        return _validate_fetch_config(v)


class WatchUpdate(BaseModel):
    """Schema for updating a watch. All fields optional. URL is immutable after creation."""

    name: str | None = None
    # url intentionally omitted — URL cannot change; delete and recreate to change
    content_type: ContentType | None = None
    fetch_config: dict | None = None
    schedule_config: dict | None = None
    is_active: bool | None = None
    effective_url: HttpUrlStr | None = None
    effective_domain: str | None = Field(default=None, max_length=253)

    @field_validator("fetch_config")
    @classmethod
    def validate_fetch_config(cls, v: dict | None) -> dict | None:
        return _validate_fetch_config(v)


class WatchResponse(BaseModel):
    """Schema for returning a watch."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    url: str
    content_type: ContentType
    fetch_config: dict
    schedule_config: dict
    is_active: bool
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    effective_url: str | None = None
    effective_domain: str | None = None
