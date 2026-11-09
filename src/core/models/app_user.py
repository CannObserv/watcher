"""AppUser model — identity anchor keyed by exe.dev user ID."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base


class AppUser(Base):
    """One row per exe.dev user, upserted on each dashboard login until #377.

    Nothing writes it since the dashboard went, and nothing reads it since #378
    deleted the API keys that referenced it; the table goes in #380.
    """

    __tablename__ = "app_users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
