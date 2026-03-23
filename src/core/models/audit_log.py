"""AuditLog model — immutable record of every system operation."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class EventType:
    """String constants for audit log event_type values."""

    WATCH_CREATED = "watch.created"
    WATCH_UPDATED = "watch.updated"
    WATCH_DEACTIVATED = "watch.deactivated"
    WATCH_DELETED = "watch.deleted"
    CHECK_SNAPSHOT_CREATED = "check.snapshot_created"
    CHECK_NO_CHANGE = "check.no_change"
    CHECK_FETCH_FAILED = "check.fetch_failed"
    NOTIFICATION_DISPATCHED = "notification.dispatched"
    NOTIFICATION_CONFIG_CREATED = "notification_config.created"
    NOTIFICATION_CONFIG_DELETED = "notification_config.deleted"
    PROFILE_CREATED = "profile.created"
    PROFILE_DELETED = "profile.deleted"


class AuditLog(Base):
    """Immutable audit log entry."""

    __tablename__ = "audit_log"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    event_type: Mapped[str] = mapped_column(String(100))
    watch_id: Mapped[ULID | None] = mapped_column(
        ULIDType, ForeignKey("watches.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("payload", {})
        super().__init__(**kwargs)
