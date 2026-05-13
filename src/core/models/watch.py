"""Watch model — links an InfoItem to per-watch scheduling and notification metadata."""

import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

# Cross-schema FK resolution stub.
#
# ``watches.info_item_id`` references ``information.info_items.info_item_id``.
# The Information service owns ``info_items`` on its own DeclarativeBase, so
# Watcher's ``Base.metadata`` cannot resolve the FK target on its own.
# Register a stub Table here exposing only the referenced primary key column.
# Watcher never creates or drops this table — production DDL lives in the
# Information service's Alembic root, and ``alembic/env.py`` filters
# non-public schemas out of autogenerate. The stub exists purely so
# SQLAlchemy can compile the cross-schema FK at import time.
Table(
    "info_items",
    Base.metadata,
    Column("info_item_id", ULIDType, primary_key=True),
    schema="information",
)

# Cross-schema FK resolution stub for info_sources (mirrors info_items pattern).
Table(
    "info_sources",
    Base.metadata,
    Column("info_source_id", ULIDType, primary_key=True),
    schema="information",
)


class ContentType(enum.StrEnum):
    """Supported content types for monitoring."""

    HTML = "html"
    PDF = "pdf"
    FILE = "file"


class WatchHealthStatus(enum.StrEnum):
    """Last known health state of a watch, updated after each check."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


class Watch(Base, TimestampMixin):
    """A URL to monitor for changes."""

    __tablename__ = "watches"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    info_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("information.info_items.info_item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    info_source_id: Mapped[ULID | None] = mapped_column(
        ULIDType,
        ForeignKey("information.info_sources.info_source_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[ContentType] = mapped_column(String(20))
    schedule_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    domain_suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    last_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    effective_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    effective_domain: Mapped[str | None] = mapped_column(String(253), nullable=True, default=None)
    tags: Mapped[list[str] | None] = mapped_column(
        ARRAY(String),
        nullable=True,
        default=None,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    health_status: Mapped[WatchHealthStatus] = mapped_column(
        String(10),
        default=WatchHealthStatus.UNKNOWN,
        server_default="unknown",
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("schedule_config", {})
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("is_archived", False)
        kwargs.setdefault("domain_suspended", False)
        kwargs.setdefault("health_status", WatchHealthStatus.UNKNOWN)
        super().__init__(**kwargs)

    @validates("content_type")
    def validate_content_type(self, _key: str, value: str | ContentType) -> ContentType:
        """Coerce string values to ContentType enum."""
        if isinstance(value, ContentType):
            return value
        try:
            return ContentType(value)
        except ValueError as exc:
            raise ValueError(f"Invalid content_type: {value!r}") from exc

    @validates("health_status")
    def validate_health_status(
        self, _key: str, value: str | WatchHealthStatus
    ) -> WatchHealthStatus:
        """Coerce string values to WatchHealthStatus enum."""
        if isinstance(value, WatchHealthStatus):
            return value
        try:
            return WatchHealthStatus(value)
        except ValueError as exc:
            raise ValueError(f"Invalid health_status: {value!r}") from exc
