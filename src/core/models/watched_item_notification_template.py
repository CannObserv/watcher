"""WatchedItemNotificationTemplate — InfoItem-level notification template, inherited by Watches."""

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class WatchedItemNotificationTemplate(Base, TimestampMixin):
    """Notification template attached to a WatchedItem.

    Mirrors `WatchNotificationConfig` in shape but lives one level up. At
    dispatch time the resolved set for a Watch is the union of its parent
    WatchedItem's templates and its own per-Watch configs (Approach B in
    the InfoItem-first design). Editing a template propagates immediately
    to all child Watches via live inheritance.
    """

    __tablename__ = "watched_item_notification_templates"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("watched_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    channel_hint: Mapped[str] = mapped_column(String(50), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        default=list,
        server_default="{change_detected}",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # none_as_null=True: None persists as SQL NULL, not JSONB 'null' (#198).
    content_config: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True, default=None
    )
    remote_channel_id: Mapped[str | None] = mapped_column(String(26), nullable=True, default=None)

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults."""
        kwargs.setdefault("events", ["change_detected"])
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
