"""Test-only ORM mirror of the Archiver service's `information` schema.

The production models live in `/home/exedev/archiver/src/core/models/`.
Watcher tests need to seed real `info_items` / `info_specs` rows so the
cross-schema FK from `watches.info_item_id` resolves; these declarations
exist solely as test scaffolding.

If the Archiver schema changes, mirror the change here. Production watcher
code never imports from this module.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from ulid import ULID

from src.core.models.base import ULIDType, generate_ulid


class InformationTestBase(DeclarativeBase):
    """Separate DeclarativeBase from watcher's `Base` to avoid metadata collision."""


class InfoItem(InformationTestBase):
    __tablename__ = "info_items"
    __table_args__ = {"schema": "information"}

    info_item_id: Mapped[ULID] = mapped_column(ULIDType(), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )


class InfoSpec(InformationTestBase):
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
