"""PendingArchiverSync model — outbox for SourceRevision POSTs to Archiver."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
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
    # Retired with the scratch cache (#253). Nothing writes these; they are
    # released rather than dropped because no single deploy order makes a drop
    # of a NOT NULL column safe — see migration 32140463c26c. Contract step
    # pending.
    content_cache_uri: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    content_cache_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # --- observation provenance, for source_revision_observed (#253) ---
    # Snapshotted from the correlated content.blobs fact at enqueue time rather
    # than joined from fetch_commands at drain time: the command row's lifecycle
    # is not this row's, and the apply path holds the values already. All
    # nullable while the HTTP POST path still drains off content_cache_uri —
    # rows written before the publisher lands legitimately have none.
    command_id: Mapped[str | None] = mapped_column(String(26), nullable=True, default=None)
    blob_uri: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    blob_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # What the origin served, echoed from the blob fact's normalized media_type.
    source_media_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # ...of the EXTRACTED content, which is a different thing (see the wire's
    # own note on the pair). Not nullable in intent — it is always in hand — but
    # nullable in the column so the migration needs no backfill of invented data.
    content_media_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    spec_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Terminal state for a row that can never publish (#253): stamped when the
    # payload is unbuildable, which is deterministic — retrying reproduces it
    # exactly. `select_due` skips these, so the row stops spinning but survives
    # for post-mortem. A *transient* broker failure never lands here.
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
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
