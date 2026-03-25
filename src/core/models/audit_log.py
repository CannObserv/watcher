"""AuditLog model — immutable record of every system operation."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
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
    PROFILE_UPDATED = "profile.updated"
    PROFILE_DELETED = "profile.deleted"
    DOMAIN_CREATED = "domain.created"
    DOMAIN_UPDATED = "domain.updated"
    DOMAIN_ARCHIVED = "domain.archived"
    DOMAIN_RESTORED = "domain.restored"
    DOMAIN_DELETED = "domain.deleted"


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


def audit(
    session: AsyncSession,
    event_type: str,
    watch_id: Any = None,
    **payload: Any,
) -> "AuditLog":
    """Create an AuditLog entry and add it to the session.

    Args:
        session: The active database session.
        event_type: The event type string (use EventType constants).
        watch_id: Optional watch ULID to associate with the entry.
        **payload: Arbitrary keyword arguments stored as the entry payload.

    Returns:
        The newly created AuditLog instance (already added to session).
    """
    entry = AuditLog(event_type=event_type, watch_id=watch_id, payload=payload)
    session.add(entry)
    return entry
