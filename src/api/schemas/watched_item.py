"""Pydantic schemas for WatchedItem and ChangeRevision."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from src.api.schemas.types import HttpUrlStr, ULIDStr
from src.core.media_type import resolve_dispatch_essence
from src.core.models.watched_item import CONTENT_MEDIA_TYPE_MAX_LEN, WatchHealthStatus


class WatchedItemCreate(BaseModel):
    """Create a WatchedItem via ``POST /api/v1/watched-items``.

    Two creation paths:
    - **InfoItem-linked** (``archiver_info_item_id`` provided): the InfoItem's existence
      is validated via the Archiver SDK (NotFound → 422); name defaults to the
      InfoItem's name when omitted.
    - **URL-only** (``url`` provided, no ``archiver_info_item_id``): the URL is probed
      for ``effective_url`` + ``domain_name``; name defaults to the probed
      domain. Produces a WatchedItem with ``archiver_info_item_id=None`` (#185 Phase A).

    At least one of ``archiver_info_item_id`` or ``url`` is required.

    ``source_specs`` seeds the local pipeline extraction config. Optional at
    create time; updatable later via PATCH.

    ``content_media_type`` is normally auto-detected from the first successful
    fetch (#168); supplying it here pre-seeds an operator override.
    """

    archiver_info_item_id: ULIDStr | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    default_schedule_config: dict | None = None
    content_media_type: str | None = Field(None, max_length=CONTENT_MEDIA_TYPE_MAX_LEN)
    default_tags: list[str] | None = None
    url: HttpUrlStr | None = None
    source_specs: list[dict] | None = None
    archiver_info_source_id: str | None = Field(None, min_length=1, max_length=26)

    @model_validator(mode="after")
    def _require_anchor(self) -> "WatchedItemCreate":
        if not self.archiver_info_item_id and not self.url:
            raise ValueError("At least one of archiver_info_item_id or url is required")
        return self


class WatchedItemPatch(BaseModel):
    """Partial update to a WatchedItem. All fields optional.

    ``effective_url`` is set directly without re-probing — Archiver is the
    authoritative source for URL succession.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    default_schedule_config: dict | None = None
    content_media_type: str | None = Field(None, max_length=CONTENT_MEDIA_TYPE_MAX_LEN)
    default_tags: list[str] | None = None
    effective_url: HttpUrlStr | None = None
    source_specs: list[dict] | None = None
    archiver_info_source_id: str | None = Field(None, min_length=1, max_length=26)

    @model_validator(mode="after")
    def _reject_explicit_null(self) -> "WatchedItemPatch":
        """Reject explicit null for NOT NULL DB columns; omitting the field is fine."""
        for field in ("name", "is_active", "effective_url", "source_specs"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit the field to leave it unchanged")
        return self


class WatchedItemResponse(BaseModel):
    """Single WatchedItem record.

    ``archiver_info_item_id`` is null for WatchedItems created via the dashboard
    (URL-first, no InfoItem required). API-created WatchedItems always have it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    archiver_info_item_id: ULIDStr | None = None
    name: str
    description: str | None
    is_active: bool
    archived_at: datetime | None
    last_reviewed_at: datetime | None
    last_checked_at: datetime | None
    last_changed_at: datetime | None
    health_status: WatchHealthStatus
    default_schedule_config: dict | None
    content_media_type: str | None
    default_tags: list[str] | None
    effective_url: str
    source_specs: list[dict]
    archiver_info_source_id: str | None = None
    domain_name: str | None = None
    domain_suspended: bool = False
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_type_essence(self) -> str | None:
        """The resolved extractor-dispatch essence — the same value the pipeline
        dispatches on (`resolve_dispatch_essence`): the observed/overridden
        ``content_media_type`` essence, with a URL-extension tiebreaker for
        mislabeled (octet-stream/text-plain/absent) headers. Computed, not stored
        (#168), so it always reflects the actual dispatch decision."""
        return resolve_dispatch_essence(self.content_media_type, self.effective_url)


class ChangeRevisionResponse(BaseModel):
    """One ChangeRevision record for a WatchedItem."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watched_item_id: ULIDStr
    content_fingerprint: str
    captured_at: datetime
    content_size_bytes: int | None
    archiver_revision_id: ULIDStr | None
    schema_version: int
