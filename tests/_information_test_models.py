"""Test-only ORM mappers for the Archiver service's `information` schema.

The schema itself (table DDL, indexes, constraints) is provisioned in tests
by running Archiver's own alembic migrations against the test database — see
``_apply_archiver_migrations`` in ``tests/conftest.py``. These class
declarations are *mappers*, not DDL: they bind to tables that already exist
and let watcher tests use ergonomic ORM inserts/queries.

Production watcher code never imports from this module.

Drift contract: column names + types here must intersect with the production
schema. If Archiver adds a NOT NULL column without a server default, inserts
through these mappers will start failing — that's the intended signal to
update this file (or the test that needs the new column).
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
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


class InfoSource(InformationTestBase):
    """Mapper-only for ``information.info_sources`` (DDL owned by Archiver)."""

    __tablename__ = "info_sources"
    __table_args__ = {"schema": "information"}

    info_source_id: Mapped[ULID] = mapped_column(
        ULIDType(), primary_key=True, default=generate_ulid
    )
    parent_info_source_id: Mapped[ULID | None] = mapped_column(
        ULIDType(),
        ForeignKey("information.info_sources.info_source_id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )


class InfoItemSource(InformationTestBase):
    """Mapper-only for ``information.info_item_sources`` (DDL owned by Archiver)."""

    __tablename__ = "info_item_sources"
    __table_args__ = {"schema": "information"}

    info_item_id: Mapped[ULID] = mapped_column(
        ULIDType(),
        ForeignKey("information.info_items.info_item_id", ondelete="CASCADE"),
        primary_key=True,
    )
    info_source_id: Mapped[ULID] = mapped_column(
        ULIDType(),
        ForeignKey("information.info_sources.info_source_id"),
        primary_key=True,
    )
    role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
