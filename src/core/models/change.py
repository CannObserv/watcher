"""Change model — detected differences between consecutive snapshots."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class Change(Base):
    """A detected change between two snapshots of the same watch."""

    __tablename__ = "changes"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id", ondelete="CASCADE"))
    previous_snapshot_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("snapshots.id", ondelete="CASCADE")
    )
    current_snapshot_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("snapshots.id", ondelete="CASCADE")
    )
    change_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    significance: Mapped[float | None] = mapped_column(Float, nullable=True)
    visual_change_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    published_to_bus_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bus_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    info_item_id: Mapped[ULID | None] = mapped_column(
        ULIDType,
        nullable=True,
        index=True,
        default=None,
    )
    info_spec_id: Mapped[ULID | None] = mapped_column(
        ULIDType,
        nullable=True,
        default=None,
    )
    previous_fingerprint: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
    )
    current_fingerprint: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("change_metadata", {})
        super().__init__(**kwargs)
