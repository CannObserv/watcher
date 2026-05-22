"""Watch model — operator-watchable content target within a WatchedItem subscription."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

if TYPE_CHECKING:
    from src.core.models.watched_item import WatchedItem


class ContentType(enum.StrEnum):
    """Supported content types for monitoring."""

    HTML = "html"
    PDF = "pdf"
    FILE = "file"


class WatchHealthStatus(enum.StrEnum):
    """Last known health state of a watch, updated after each check."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


class Watch(Base, TimestampMixin):
    """A content target within a WatchedItem subscription.

    `target_info_source_id` discriminates the target kind:
    * NULL — the InfoItem's primary content. Cross-check bindings produce
      selector-rot signal but do not change the Watch's identity.
    * non-NULL — a specific `sub_aspect`-bound fragment InfoSource.

    Scheduling is owned by the parent WatchedItem; the fetch happens once per
    InfoItem per cycle. Notifications, tags, and content_type may be overridden
    per Watch over the WatchedItem's defaults via `src/core/watches/resolution.py`.
    """

    __tablename__ = "watches"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    info_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        nullable=False,
        index=True,
    )
    target_info_source_id: Mapped[ULID | None] = mapped_column(
        ULIDType,
        nullable=True,
        index=True,
    )
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("watched_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    watched_item: Mapped["WatchedItem"] = relationship("WatchedItem", lazy="joined")

    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[ContentType | None] = mapped_column(
        String(20),
        nullable=True,
        default=None,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    domain_suspended: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    last_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    effective_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    effective_domain: Mapped[str | None] = mapped_column(String(253), nullable=True, default=None)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True, default=None)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    health_status: Mapped[WatchHealthStatus] = mapped_column(
        String(10),
        default=WatchHealthStatus.UNKNOWN,
        server_default="unknown",
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("is_archived", False)
        kwargs.setdefault("domain_suspended", False)
        kwargs.setdefault("health_status", WatchHealthStatus.UNKNOWN)
        super().__init__(**kwargs)

    @validates("content_type")
    def validate_content_type(
        self, _key: str, value: str | ContentType | None
    ) -> ContentType | None:
        """Coerce string values to ContentType enum; allow NULL."""
        if value is None:
            return None
        if isinstance(value, ContentType):
            return value
        try:
            return ContentType(value)
        except ValueError as exc:
            raise ValueError(f"Invalid content_type: {value!r}") from exc

    @validates("health_status")
    def validate_health_status(
        self, _key: str, value: str | WatchHealthStatus
    ) -> WatchHealthStatus:
        """Coerce string values to WatchHealthStatus enum."""
        if isinstance(value, WatchHealthStatus):
            return value
        try:
            return WatchHealthStatus(value)
        except ValueError as exc:
            raise ValueError(f"Invalid health_status: {value!r}") from exc
