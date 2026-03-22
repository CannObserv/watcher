"""NotificationConfig model — per-watch notification channel configuration."""

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class NotificationConfig(Base, TimestampMixin):
    """A notification channel configuration for a specific watch."""

    __tablename__ = "notification_configs"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __init__(self, **kwargs):
        """Set Python-side defaults."""
        kwargs.setdefault("config", {})
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
