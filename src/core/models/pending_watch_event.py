"""PendingWatchEvent model — outbox for the ``info.watch-events`` facts (#369)."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class PendingWatchEvent(Base):
    """One edge fact waiting to be published on ``info.watch-events``.

    Staged in the transaction that records its edge — a change, the edge into
    error, the recovery — so the fact exists exactly when the state change
    does. The drain validates ``payload`` against the Emit twin ``event_type``
    names, publishes it, then deletes the row.

    **``id`` is the fact's ``event_id``** (the envelope key): minted once at
    the edge, so a redelivery carries the stored one. **``payload`` is the
    fact's field map**, so a new kind (#371's ``check_rejected``) needs no
    schema change — only an entry in ``WATCH_EVENT_EMITS``.

    **No foreign key to the item.** A fact outlives a revoke: the error edge
    happened, and Archiver closes the episode on the revoke itself. The id is
    Archiver's ``info_item_id``, kept beside the payload for queries.
    """

    __tablename__ = "pending_watch_events"

    # The drain reads in id order and stops at the first row it cannot send
    # (head-of-line, so facts reach the stream in the order their edges
    # committed). Partial on the drain's predicate: a dead-lettered row is
    # terminal and never a candidate.
    __table_args__ = (
        Index(
            "ix_pending_watch_events_due",
            "id",
            postgresql_where=text("dead_lettered_at IS NULL"),
        ),
    )

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    info_item_id: Mapped[str] = mapped_column(String(26), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Terminal state for a payload the Emit refuses — deterministic, so the
    # drain stamps it at once and skips past it. A transient broker failure
    # never lands here.
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
