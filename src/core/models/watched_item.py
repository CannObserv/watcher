"""WatchedItem model — monitored content target, optionally linked to an Archiver InfoItem."""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class ContentType(enum.StrEnum):
    """Supported content types for monitoring."""

    HTML = "html"
    PDF = "pdf"
    FILE = "file"


class WatchHealthStatus(enum.StrEnum):
    """Health state of a WatchedItem, updated after each check cycle."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


class WatchedItem(Base, TimestampMixin):
    """Operator's subscription to a monitored content target.

    Owns the schedule/content defaults applied at read time via the resolution
    chain (WatchedItem default → system default). Post-#191 the WatchedItem is
    the single monitored entity; there is no per-Watch override layer.

    `archiver_info_item_id` links to an Archiver InfoItem (cross-schema reference to
    `information.info_items.info_item_id`). Nullable — standalone WatchedItems
    with no InfoItem reference are allowed; partial unique index enforces
    uniqueness when set.

    `effective_url` and `source_specs` are set at create time and drive
    the pipeline directly, without an Archiver SDK call per cycle.

    `domain_name` is the hostname of the primary URL, set at create time.
    `domain_suspended` is set True when the parent Domain is deactivated.
    """

    __tablename__ = "watched_items"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    archiver_info_item_id: Mapped[ULID | None] = mapped_column(ULIDType, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    last_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # none_as_null=True so an omitted/None config persists as SQL NULL, not the
    # JSONB 'null' literal — keeps `IS NULL` queries correct (#198).
    default_schedule_config: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True, default=None
    )
    default_content_type: Mapped[ContentType | None] = mapped_column(
        String(20), nullable=True, default=None
    )
    default_tags: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True, default=None
    )
    domain_name: Mapped[str | None] = mapped_column(
        String(253),
        ForeignKey("domains.name", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )
    domain_suspended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Pipeline state — populated at create time; updated by pipeline.
    effective_url: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_specs: Mapped[list] = mapped_column(
        ARRAY(JSONB(astext_type=Text())),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::jsonb[]"),
    )
    archiver_info_source_id: Mapped[str | None] = mapped_column(
        String(26), nullable=True, default=None
    )
    last_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    health_status: Mapped[WatchHealthStatus] = mapped_column(
        String(10),
        nullable=True,
        default=WatchHealthStatus.UNKNOWN,
        server_default="unknown",
    )

    __table_args__ = (
        Index(
            "ix_watched_items_archiver_info_item_id",
            "archiver_info_item_id",
            unique=True,
            postgresql_where=text("archiver_info_item_id IS NOT NULL"),
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("domain_suspended", False)
        kwargs.setdefault("effective_url", "")
        kwargs.setdefault("source_specs", [])
        kwargs.setdefault("health_status", WatchHealthStatus.UNKNOWN)
        super().__init__(**kwargs)

    @validates("default_content_type")
    def validate_default_content_type(
        self, _key: str, value: str | ContentType | None
    ) -> ContentType | None:
        """Coerce string values to ContentType enum, allow NULL."""
        if value is None:
            return None
        if isinstance(value, ContentType):
            return value
        try:
            return ContentType(value)
        except ValueError as exc:
            raise ValueError(f"Invalid default_content_type: {value!r}") from exc
