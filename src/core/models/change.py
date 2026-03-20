"""Change model — detected differences between consecutive snapshots."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class Change(Base):
    """A detected change between two snapshots of the same watch."""

    __tablename__ = "changes"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id"))
    previous_snapshot_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("snapshots.id"))
    current_snapshot_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("snapshots.id"))
    change_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("change_metadata", {})
        super().__init__(**kwargs)
