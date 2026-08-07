"""Pydantic schemas for WatchedItem and ChangeRevision."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from src.api.schemas.types import HttpUrlStr, ULIDStr
from src.core.media_type import resolve_dispatch_essence
from src.core.models.watched_item import CONTENT_MEDIA_TYPE_MAX_LEN, WatchHealthStatus


class WatchedItemCreate(BaseModel):
    """Create a WatchedItem via ``POST /api/v1/watched-items``.

    One creation path (#251): every WatchedItem is an Archiver InfoItem being
    watched. ``archiver_info_item_id`` is validated via the Archiver SDK
    (NotFound → 422) and the name defaults to the InfoItem's name when omitted;
    ``url`` is the InfoSource URL Archiver is authoritative for (stored as
    ``effective_url``, never re-probed); ``archiver_info_source_id`` identifies
    the InfoSource that observed revisions are posted back to. All three are
    required — the URL-only path was rolled back with bare-URL WatchedItems.

    ``source_specs`` seeds the local pipeline extraction config. Optional at
    create time; updatable later via PATCH.

    ``content_media_type`` is normally auto-detected from the first successful
    fetch (#168); supplying it here pre-seeds an operator override.
    """

    archiver_info_item_id: ULIDStr
    url: HttpUrlStr
    archiver_info_source_id: str = Field(min_length=1, max_length=26)
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    default_schedule_config: dict | None = None
    content_media_type: str | None = Field(None, max_length=CONTENT_MEDIA_TYPE_MAX_LEN)
    default_tags: list[str] | None = None
    source_specs: list[dict] | None = None


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
        for field in (
            "name",
            "is_active",
            "effective_url",
            "source_specs",
            "archiver_info_source_id",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit the field to leave it unchanged")
        return self


class WatchedItemResponse(BaseModel):
    """Single WatchedItem record.

    ``archiver_info_item_id`` and ``archiver_info_source_id`` are always
    present — every WatchedItem is linked to an Archiver InfoItem (#251).
    """

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    archiver_info_item_id: ULIDStr
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
    archiver_info_source_id: str
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
