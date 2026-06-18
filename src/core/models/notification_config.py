"""WatchNotificationConfig model — per-watch notifier-channel pointer."""

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class WatchNotificationConfig(Base, TimestampMixin):
    """A notifier-channel pointer scoped to a WatchedItem (#191).

    `remote_channel_id` is the notifier-service channel ULID; the notifier
    owns the actual delivery target. `channel_hint` carries a human-readable
    scheme tag (e.g. "slack", "mailto") for display only. `events` is the
    list of WatchEventType codes this config opts into.
    """

    __tablename__ = "watch_notification_configs"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("watched_items.id", ondelete="CASCADE")
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

    def __init__(self, **kwargs):
        """Set Python-side defaults."""
        kwargs.setdefault("events", ["change_detected"])
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
