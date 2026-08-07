"""PendingArchiverSync model — outbox for SourceRevision POSTs to Archiver."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class PendingArchiverSync(Base):
    """A ChangeRevision waiting to be POSTed to Archiver as a SourceRevision.

    Inserted on every detected fingerprint change (#251 — every WatchedItem
    carries an `archiver_info_source_id` to post against). The drain worker
    reads `content_cache_uri`,
    sends the bytes to Archiver, then back-populates
    `change_revisions.archiver_revision_id` on success.
    """

    __tablename__ = "pending_archiver_sync"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    change_revision_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("change_revisions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("watched_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_cache_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_cache_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
