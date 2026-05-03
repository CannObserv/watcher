"""Pydantic IO schemas for InfoSpec endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InfoSpecCreate(BaseModel):
    document: dict[str, Any]
    priority: int | None = Field(default=None, ge=1)


class InfoSpecOut(BaseModel):
    info_spec_id: str
    info_item_id: str
    schema_version: int
    document: dict[str, Any]
    priority: int
    active: bool
    created_at: datetime


class InfoSpecPatch(BaseModel):
    priority: int | None = Field(default=None, ge=1)
    active: bool | None = None
