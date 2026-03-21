"""Pydantic schemas for temporal profile CRUD."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import ULIDStr
from src.core.models.temporal_profile import PostAction, ProfileType


class ProfileRuleItem(BaseModel):
    """A single rule: {days_before, interval}."""

    days_before: int
    interval: str


class ProfileCreate(BaseModel):
    """Schema for creating a temporal profile."""

    profile_type: ProfileType
    reference_date: date | None = None
    date_range_start: date | None = None
    date_range_end: date | None = None
    rules: list[ProfileRuleItem] = Field(default_factory=list)
    post_action: PostAction


class ProfileResponse(BaseModel):
    """Schema for returning a temporal profile."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watch_id: ULIDStr
    profile_type: ProfileType
    reference_date: date | None
    date_range_start: date | None
    date_range_end: date | None
    rules: list
    post_action: PostAction
    is_active: bool
    created_at: datetime
    updated_at: datetime
