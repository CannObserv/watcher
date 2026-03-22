"""Snapshot and SnapshotChunk models — content capture records."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class Snapshot(Base):
    """A single fetch and extraction of a watched URL."""

    __tablename__ = "snapshots"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id", ondelete="CASCADE"))
    content_hash: Mapped[str] = mapped_column(String(64))
    simhash: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(Text)
    text_path: Mapped[str] = mapped_column(Text)
    storage_backend: Mapped[str] = mapped_column(String(20), default="local")
    chunk_count: Mapped[int] = mapped_column(Integer)
    text_bytes: Mapped[int] = mapped_column(BigInteger)
    fetch_duration_ms: Mapped[int] = mapped_column(Integer)
    fetcher_used: Mapped[str] = mapped_column(String(50))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class SnapshotChunk(Base):
    """A structural chunk within a snapshot (page, section, row range)."""

    __tablename__ = "snapshot_chunks"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    snapshot_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("snapshots.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(SmallInteger)
    chunk_type: Mapped[str] = mapped_column(String(20))
    chunk_label: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    simhash: Mapped[int] = mapped_column(BigInteger)
    char_count: Mapped[int] = mapped_column(Integer)
    excerpt: Mapped[str] = mapped_column(Text)
