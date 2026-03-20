"""Watch model — a URL to monitor for changes."""

import enum

from sqlalchemy import Boolean, Enum, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class ContentType(enum.StrEnum):
    """Supported content types for monitoring."""

    HTML = "html"
    PDF = "pdf"
    FILE = "file"


class Watch(Base, TimestampMixin):
    """A URL to monitor for changes."""

    __tablename__ = "watches"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[ContentType] = mapped_column(Enum(ContentType, native_enum=False))
    fetch_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    schedule_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("fetch_config", {})
        kwargs.setdefault("schedule_config", {})
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
