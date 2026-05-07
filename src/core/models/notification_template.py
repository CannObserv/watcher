"""Notification template library models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class NotificationTemplate(TimestampMixin, Base):
    """Shared, reusable notification configuration template."""

    __tablename__ = "notification_templates"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_hint: Mapped[str] = mapped_column(String(50), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        server_default=text("ARRAY['change_detected']::varchar[]"),
    )
    is_global_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    content_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)
    remote_channel_id: Mapped[str | None] = mapped_column(String(26), nullable=True, default=None)


class WatchNcRef(Base):
    """Junction: NotificationTemplate assigned to a Watch."""

    __tablename__ = "watch_nc_refs"

    watch_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("watches.id", ondelete="CASCADE"), primary_key=True
    )
    template_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("notification_templates.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DomainNcRef(Base):
    """Junction: NotificationTemplate that is a default for a Domain."""

    __tablename__ = "domain_nc_refs"

    domain_name: Mapped[str] = mapped_column(
        String(253), ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
    )
    template_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("notification_templates.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
