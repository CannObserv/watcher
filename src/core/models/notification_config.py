"""WatchNotificationConfig model — per-watch Apprise notification target."""

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class WatchNotificationConfig(Base, TimestampMixin):
    """A single Apprise notification target for a specific watch.

    apprise_url stores the Fernet-encrypted Apprise URL string (e.g. slack://T/A/T/#ops).
    channel_hint stores the URL scheme for display purposes (e.g. "slack", "mailto").
    events is the list of WatchEventType codes this config opts into.
    """

    __tablename__ = "watch_notification_configs"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    apprise_url: Mapped[str] = mapped_column(Text, nullable=False)
    channel_hint: Mapped[str] = mapped_column(String(50), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        default=list,
        server_default="{change_detected}",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    content_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)

    def __init__(self, **kwargs):
        """Set Python-side defaults."""
        kwargs.setdefault("events", ["change_detected"])
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
