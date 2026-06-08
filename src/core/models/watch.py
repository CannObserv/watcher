"""Watch model — operator-watchable content target within a WatchedItem subscription."""

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
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
    """Health state of a watched item, updated after each check cycle.

    Kept in watch.py for import stability (WatchedItem and tasks both use it).
    """

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


class Watch(Base, TimestampMixin):
    """A content target within a WatchedItem subscription.

    #185 Phase A step 6: per-Watch tracking columns dropped. State that was
    per-Watch (last_checked_at, last_changed_at, health_status, effective_url,
    info_item_id, target_info_source_id) now lives on the parent WatchedItem.

    Remaining per-Watch fields: identity (id, watched_item_id), display
    (name, content_type, description, tags), lifecycle flags (is_active,
    is_archived, suspended_by_domain), and timestamps (created_at, updated_at).
    """

    __tablename__ = "watches"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
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
    suspended_by_domain: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True, default=None)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("is_archived", False)
        kwargs.setdefault("suspended_by_domain", False)
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
