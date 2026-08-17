"""Pydantic schemas for WatchedItem and ChangeRevision."""

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from src.api.schemas.types import HttpUrlStr, ULIDRefStr, ULIDStr
from src.core.media_type import resolve_dispatch_essence
from src.core.models.watched_item import CONTENT_MEDIA_TYPE_MAX_LEN, WatchHealthStatus
from src.core.scheduling.cadence import validate_optional_schedule_config


class WatchedItemCreate(BaseModel):
    """Create a WatchedItem via ``POST /api/v1/watched-items``.

    Every WatchedItem is an Archiver InfoItem being watched (#251).
    ``archiver_info_item_id`` is **not** validated against Archiver — that HTTP
    call was Watcher's last and went with the SDK (#254); the ``info.registry``
    announcement for the key is the authority, and reconciles whatever this
    route creates. ``url`` is the InfoSource URL Archiver is authoritative for
    (stored as ``effective_url``, never re-probed); ``archiver_info_source_id``
    identifies the InfoSource that observed revisions are reported against. All
    three are required — the URL-only path was rolled back with bare-URL
    WatchedItems.

    ``name`` defaults to a host+path derivation of ``url`` when omitted; a name
    supplied here survives reconciliation, which never overwrites one.

    ``source_specs`` seeds the local pipeline extraction config. **Required and
    non-empty** (#260): a WatchedItem with no specs has no defined extraction,
    and the full-page default it used to inherit was never ratified. Archiver,
    the only caller, always has them — its registry refuses to announce a source
    as live without non-empty ``source_specs`` — so this closes a state nobody
    could legitimately reach. Updatable later via PATCH, which holds the same
    floor.

    ``content_media_type`` is normally auto-detected from the first successful
    fetch (#168); supplying it here pre-seeds an operator override.
    """

    archiver_info_item_id: ULIDRefStr
    url: HttpUrlStr
    archiver_info_source_id: ULIDRefStr
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    default_schedule_config: dict | None = None
    content_media_type: str | None = Field(None, max_length=CONTENT_MEDIA_TYPE_MAX_LEN)
    default_tags: list[str] | None = None
    source_specs: list[dict] = Field(..., min_length=1)

    @field_validator("default_schedule_config")
    @classmethod
    def _cadence(cls, v: dict | None) -> dict | None:
        """Reject a malformed or intervalless cadence at the write boundary.

        The same rule, via the same helper, as the Domain boundary (#205 /
        `DomainPatch._cadence`) — #254 CR-16/19 verified the direction against
        cannobserv#324: delegation has exactly one spelling (`None`/omit), never
        an empty document, and this is the only place that can hold the line —
        `schedule_tick` resolves every item in one task, so an unparseable
        stored interval stops scheduling for the whole system, not one row.
        """
        return validate_optional_schedule_config(v)


class WatchedItemPatch(BaseModel):
    """Partial update to a WatchedItem. All fields optional.

    ``effective_url`` is set directly without re-probing — Archiver is the
    authoritative source for URL succession.

    ``source_specs``, when supplied, must be non-empty — the create-time floor
    (#260) is worth nothing if a PATCH can empty the list again.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    default_schedule_config: dict | None = None
    content_media_type: str | None = Field(None, max_length=CONTENT_MEDIA_TYPE_MAX_LEN)
    default_tags: list[str] | None = None
    effective_url: HttpUrlStr | None = None
    source_specs: list[dict] | None = Field(None, min_length=1)
    archiver_info_source_id: ULIDRefStr | None = None

    @field_validator("default_schedule_config")
    @classmethod
    def _cadence(cls, v: dict | None) -> dict | None:
        """Same rule as create — see ``WatchedItemCreate._cadence``."""
        return validate_optional_schedule_config(v)

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
    # The info.registry generation this row has applied (#254); NULL until the
    # first announcement. Exposed because it gates the DELETE 409 — without it a
    # caller cannot tell whether a delete will be accepted without attempting it.
    applied_generation: int | None = None
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
    """One ChangeRevision record for a WatchedItem.

    ``archiver_revision_id`` was removed in #253: Archiver allocates the registry
    id on its side of ``content.revisions`` and never reports it back, so the
    field could only ever have been null. A **breaking** response change, taken
    deliberately over shipping a permanently-null field that reads as "not synced
    yet". The column survives on the model, holding the 23 ids captured while the
    HTTP write path existed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watched_item_id: ULIDStr
    content_fingerprint: str
    captured_at: datetime
    content_size_bytes: int | None
    schema_version: int
