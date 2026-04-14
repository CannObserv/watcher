"""Watch model — a URL to monitor for changes."""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


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
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[ContentType] = mapped_column(String(20))
    fetch_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
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
    health_status: Mapped[WatchHealthStatus] = mapped_column(
        String(10),
        default=WatchHealthStatus.UNKNOWN,
        server_default="unknown",
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("fetch_config", {})
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
