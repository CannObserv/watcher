"""WatchedItem model — 1:1 mirror of an Archiver InfoItem subscription."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid
from src.core.models.watch import ContentType


class WatchedItem(Base, TimestampMixin):
    """Operator's subscription to one Archiver InfoItem.

    Owns shared defaults that child Watches inherit at read time via the
    resolution chain (Watch override → WatchedItem default → system default).

    Identity is `info_item_id` (cross-schema reference to
    `information.info_items.info_item_id`). The FK is not declared at the
    Watcher schema level — Archiver owns that table on a separate
    DeclarativeBase. Watcher trusts the Archiver SDK to validate
    info_item_id at create-time.

    `domain_name` is the hostname of the InfoItem's primary URL, set at
    Watch-create time. NULL for standalone WatchedItems with no Watches yet.
    `domain_suspended` is set to True when the parent Domain is deactivated
    and cleared on reactivation; used for UI banners and the suspension cascade.
    """

    __tablename__ = "watched_items"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    info_item_id: Mapped[ULID] = mapped_column(ULIDType, unique=True, nullable=False, index=True)
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
    default_schedule_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)
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

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("domain_suspended", False)
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
