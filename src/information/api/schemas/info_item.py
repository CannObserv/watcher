"""Pydantic IO schemas for InfoItem endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class InfoItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=200)


class InfoItemOut(BaseModel):
    info_item_id: str
    name: str
    description: str | None
    owner: str | None
    created_at: datetime
    updated_at: datetime
