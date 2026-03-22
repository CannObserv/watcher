"""TemporalProfile model — scheduling rules tied to external dates."""

import enum
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class ProfileType(enum.StrEnum):
    """Type of temporal profile controlling schedule escalation behavior."""

    EVENT = "event"
    SEASONAL = "seasonal"
    DEADLINE = "deadline"


class PostAction(enum.StrEnum):
    """Action taken after a temporal profile's date window passes."""

    REDUCE_FREQUENCY = "reduce_frequency"
    DEACTIVATE = "deactivate"
    ARCHIVE = "archive"


class TemporalProfile(Base, TimestampMixin):
    """A temporal scheduling rule tied to a specific date or date range."""

    __tablename__ = "temporal_profiles"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id", ondelete="CASCADE"))
    profile_type: Mapped[ProfileType] = mapped_column(String(20))
    reference_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_range_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_range_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    rules: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    post_action: Mapped[PostAction] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __init__(self, **kwargs):
        """Initialize with sensible defaults for rules and is_active."""
        kwargs.setdefault("rules", [])
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
