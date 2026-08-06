"""FetchCommand — outbox, pending map, and inbox for content.fetch (#241, Phase 4).

One row per issued ``ContentFetchCommand``, keyed by the ``command_id`` ULID —
the sole correlator the bus carries (issuer contract MUST-2/MUST-3: the wire is
domain-agnostic, so losing this row makes the returning fact permanently
uncorrelatable). The row is written and committed *before* the XADD
(persist-before-publish); the fact fields are upserted by the ``content.blobs``
consumer (MUST-4: at-least-once, per-emission keys — several distinct facts per
command are normal, last terminal wins).

``intent_id`` is lineage: one fetch *intent* may span several ``command_id``s
when the reaper re-issues after a silent failure (MUST-6 — a timeout means
re-issue under a fresh id, never conclude failure).

``content_fingerprint`` here is Replicator's **raw-bytes** identity — distinct
from watcher's extracted-text fingerprint on ``ChangeRevision``; the two coexist
(MUST-5: never dedupe correlation on it).
"""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType


class FetchCommandStatus(enum.StrEnum):
    """Lifecycle of one issued command."""

    PENDING_PUBLISH = "pending_publish"  # row committed, XADD not yet confirmed
    IN_FLIGHT = "in_flight"  # published; awaiting a fact
    SUCCEEDED = "succeeded"  # blob applied through the pipeline
    FAILED = "failed"  # terminal fetch_failed (or re-issue cap hit)
    SUPERSEDED = "superseded"  # a newer command for the item applied first
    EXPIRED = "expired"  # reaped after timeout; re-issued under a fresh id


# The statuses that make a command "open": they gate scheduling (no new issue
# while one is open) and are what the reaper scans.
OPEN_STATUSES = (FetchCommandStatus.PENDING_PUBLISH, FetchCommandStatus.IN_FLIGHT)


class FetchCommand(Base, TimestampMixin):
    """One issued content.fetch command and everything its facts brought back."""

    __tablename__ = "fetch_commands"
    __table_args__ = (
        # Partial: the schedule gate and the reaper scan only open rows.
        Index(
            "ix_fetch_commands_open",
            "watched_item_id",
            postgresql_where=text("status IN ('pending_publish', 'in_flight')"),
        ),
    )

    command_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("watched_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FetchCommandStatus.PENDING_PUBLISH
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    reissue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- fact fields (upserted by the content.blobs consumer) ---
    fact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    content_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    blob_uri: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    content_type_raw: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
