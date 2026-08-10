"""ChangeRevision model — local fingerprint history for a WatchedItem."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class ChangeRevision(Base):
    """One fingerprint snapshot captured for a WatchedItem in a check cycle.

    Inserted on first run (baseline) and on every subsequent fingerprint
    change. The `archiver_revision_id` is back-populated by the drain worker
    after a successful POST to Archiver.
    """

    __tablename__ = "change_revisions"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("watched_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Historical only since #253. The HTTP write path back-populated Archiver's
    # minted id so the cache sweeper could PATCH against it; both are gone, and
    # Archiver now allocates on its side of content.revisions without reporting
    # back. Nothing writes this and nothing reads it — retained rather than
    # dropped because the rows captured while that path existed are the only
    # local pointer to their registry counterparts.
    archiver_revision_id: Mapped[ULID | None] = mapped_column(ULIDType, nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
