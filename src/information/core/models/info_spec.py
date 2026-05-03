"""Information Source Specification — one way to source an InfoItem."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.information.core.models.base import Base, ULIDType, generate_ulid


class InfoSpec(Base):
    """An InfoSpec — describes one way to source a parent InfoItem.

    Document body (JSONB column) is immutable. Placement metadata
    (priority, active) is mutable.
    """

    __tablename__ = "info_specs"

    info_spec_id: Mapped[ULID] = mapped_column(ULIDType(), primary_key=True, default=generate_ulid)
    info_item_id: Mapped[ULID] = mapped_column(
        ULIDType(),
        ForeignKey("information.info_items.info_item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "uq_info_specs_active_priority_per_item",
            "info_item_id",
            "priority",
            unique=True,
            postgresql_where=text("active"),
        ),
        {"schema": "information"},
    )
