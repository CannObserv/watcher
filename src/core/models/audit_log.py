"""AuditLog model — immutable record of every system operation."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class EventType:
    """String constants for audit log event_type values."""

    # Legacy watch.* events were removed in the #191 collapse; the WATCH_*
    # constants and their stray audit_log rows were purged (pre-production noise).
    CHECK_SNAPSHOT_CREATED = "check.snapshot_created"
    CHECK_NO_CHANGE = "check.no_change"
    CHECK_FETCH_FAILED = "check.fetch_failed"
    CHECK_EXTRACTION_FAILED = "check.extraction_failed"
    NOTIFICATION_DISPATCHED = "notification.dispatched"
    NOTIFICATION_TEST = "notification.test"
    # #200: every notification target is a NotificationTemplate (any visibility);
    # the legacy notification_config.*, watch_nc.*, watched_item_template.*, and
    # domain_nc_default.* events were unified into this single set. Historical
    # audit_log rows keep their old event_type strings.
    NOTIFICATION_TEMPLATE_CREATED = "notification_template.created"
    NOTIFICATION_TEMPLATE_UPDATED = "notification_template.updated"
    NOTIFICATION_TEMPLATE_DELETED = "notification_template.deleted"
    NOTIFICATION_TEMPLATE_TESTED = "notification_template.tested"
    PROFILE_CREATED = "profile.created"
    PROFILE_UPDATED = "profile.updated"
    PROFILE_DELETED = "profile.deleted"
    DOMAIN_CREATED = "domain.created"
    DOMAIN_UPDATED = "domain.updated"
    DOMAIN_DEACTIVATED = "domain.deactivated"
    DOMAIN_ACTIVATED = "domain.activated"
    DOMAIN_ARCHIVED = "domain.archived"
    DOMAIN_RESTORED = "domain.restored"
    DOMAIN_DELETED = "domain.deleted"
    WATCHED_ITEM_THROTTLED = "watched_item.throttled"
    WATCHED_ITEM_CREATED = "watched_item.created"
    WATCHED_ITEM_UPDATED = "watched_item.updated"
    WATCHED_ITEM_ARCHIVED = "watched_item.archived"
    WATCHED_ITEM_RESTORED = "watched_item.restored"
    WATCHED_ITEM_DELETED = "watched_item.deleted"
    WATCHED_ITEM_PAUSED = "watched_item.paused"
    WATCHED_ITEM_RESUMED = "watched_item.resumed"
    WATCHED_ITEM_REVIEWED = "watched_item.reviewed"
    WATCHED_ITEM_CHECK_REQUESTED = "watched_item.check_requested"


class AuditLog(Base):
    """Immutable audit log entry."""

    __tablename__ = "audit_log"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # The WatchedItem association lives in ``payload`` (the ``watch_id`` FK was
    # retired in #191), and ``audit_log`` grows unbounded. Index the JSONB
    # text-extraction the filter queries use, plus ``created_at`` for the
    # order-by every caller applies, so the lookup never degrades to a seq scan.
    __table_args__ = (
        Index(
            "ix_audit_log_payload_watched_item_id",
            text("(payload->>'watched_item_id')"),
            created_at.desc(),
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("payload", {})
        super().__init__(**kwargs)


def audit(
    session: AsyncSession,
    event_type: str,
    **payload: Any,
) -> "AuditLog":
    """Create an AuditLog entry and add it to the session.

    Args:
        session: The active database session.
        event_type: The event type string (use EventType constants).
        **payload: Arbitrary keyword arguments stored as the entry payload.
            The associated WatchedItem is carried as ``watched_item_id`` here.

    Returns:
        The newly created AuditLog instance (already added to session).
    """
    entry = AuditLog(event_type=event_type, payload=payload)
    session.add(entry)
    return entry
