"""Watcher-side outbox for SourceRevisions awaiting POST to Archiver.

Inserted when Archiver is unreachable (network, 5xx, 401). Drain worker
retries with backoff and clears rows on success.

The `id` column doubles as the client-supplied `source_revision_id` —
Watcher allocates the ULID up-front and references the scratch file
`<id>.bin` from `content_cache_uri`.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class PendingSourceRevision(Base):
    """A SourceRevision waiting to be POSTed to Archiver."""

    __tablename__ = "pending_source_revisions"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    info_source_id: Mapped[ULID] = mapped_column(ULIDType, nullable=False, index=True)
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_cache_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_cache_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "info_source_id",
            "content_fingerprint",
            name="uq_pending_source_revisions_source_fingerprint",
        ),
    )
