"""Pydantic IO schemas for InfoItem endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InfoItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=200)
    initial_info_spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional InfoSpec document to atomically create alongside the new "
            "InfoItem at priority=1, active=True. Validated against the v1 "
            "schema before either row is written; on validation failure neither "
            "InfoItem nor InfoSpec is persisted."
        ),
    )


class InfoItemOut(BaseModel):
    info_item_id: str
    name: str
    description: str | None
    owner: str | None
    created_at: datetime
    updated_at: datetime


class InfoItemWithSpecOut(InfoItemOut):
    """InfoItem creation response that may carry the atomically-created spec ID.

    Returned by ``POST /api/v1/info-items``. ``info_spec_id`` is populated only
    when the request supplied ``initial_info_spec``; otherwise it is ``null``.
    """

    info_spec_id: str | None = None
