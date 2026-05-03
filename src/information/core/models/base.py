"""Information service SQLAlchemy declarative base + ULID type."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from ulid import ULID


class ULIDType(TypeDecorator):
    """Store ULIDs as 26-char strings."""

    impl = String(26)
    cache_ok = True

    def process_bind_param(self, value: ULID | None, dialect) -> str | None:
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value: str | None, dialect) -> ULID | None:
        if value is None:
            return None
        return ULID.from_str(value)


def generate_ulid() -> ULID:
    return ULID()


class Base(DeclarativeBase):
    """Information service declarative base — distinct from watcher's Base."""


class TimestampMixin:
    """Adds created_at / updated_at columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )
