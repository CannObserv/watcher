"""ProcessCommand — outbox, pending map, and inbox for content.process (#325).

One row per issued ``ContentProcessCommand``, keyed by the ``command_id`` ULID —
the correlator ``content.derived`` carries back (cannobserv#486). The issuer
discipline is ``fetch_commands``'s: the row is written and committed *before*
the XADD, an every-minute sweep republishes a row the broker never confirmed,
and the ``content.derived`` consumer upserts the fact fields onto it.

**One row per (blob, spec) occasion.** A fetch occasion's raw blob is derived
under one ``source_spec`` per command (design D3), so the spec fallback loop is
a *chain* of rows: an ``empty`` outcome for spec[i] is answered by a fresh row
for spec[i+1]. ``intent_id`` is the lineage across that chain and across the
reaper's re-issues; ``fetch_command_id`` names the occasion it all derives from.

**The first terminal fact wins** (#325 amendment, CannObserv/processor#17). A
lost ack makes the processor publish the same outcome again under a fresh
``occurred_at`` — a distinct envelope key, so the bus does not merge them — and
a give-up after three escaping exceptions may follow a success it could not
ack. The consumer settles the row on the first terminal fact and logs the rest.

**The ``local_*`` and ``shadow_*`` columns are shadow mode's**
(``WATCHER_EXTRACT_MODE=shadow``): local extraction still decides, and each
lineage carries what local extraction concluded for the same occasion so the
comparator can judge the processor's answer against it. They go with the local
path when the switch deletes it (#326).

``input_digest`` is Replicator's **raw-bytes** identity (bare hex), copied from
the blob fact; ``output_digest`` is the derived text's (``sha256:<hex>``), the
spelling of ``ChangeRevision.content_fingerprint`` it is compared against.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType


class ProcessCommandStatus(enum.StrEnum):
    """Lifecycle of one issued process command."""

    PENDING_PUBLISH = "pending_publish"  # row committed, XADD not yet confirmed
    IN_FLIGHT = "in_flight"  # published; awaiting a fact
    COMPLETED = "completed"  # a processing_complete fact settled it
    FAILED = "failed"  # a terminal processing_failed fact settled it
    EXPIRED = "expired"  # reaped: re-issued under a fresh id, or given up on


# Open = awaiting publish or a fact. A *positive* enumeration, for
# ``fetch_commands``' reason: a new terminal member is closed by default.
OPEN_PROCESS_STATUSES = (ProcessCommandStatus.PENDING_PUBLISH, ProcessCommandStatus.IN_FLIGHT)

# Settled by a fact; ``applied_at`` says whether the apply task has run.
SETTLED_PROCESS_STATUSES = (ProcessCommandStatus.COMPLETED, ProcessCommandStatus.FAILED)


class LocalOutcome(enum.StrEnum):
    """What local extraction concluded for the occasion (shadow mode)."""

    BASELINE = "baseline"
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    # Local extraction raised: the parser failed, or every spec was empty
    # (#258). The processor's matching answers are ``extraction_error`` and an
    # empty outcome on the last spec — the local path cannot tell them apart.
    EXTRACTION_FAILED = "extraction_failed"


class ShadowVerdict(enum.StrEnum):
    """The comparator's judgement of one lineage."""

    MATCH = "match"
    MISMATCH = "mismatch"
    # The processor never judged the bytes — its input was unreadable or
    # refused, or the lineage timed out. Coverage lost, not disagreement.
    UNCOMPARED = "uncompared"


class ProcessCommand(Base, TimestampMixin):
    """One issued content.process command and the fact that settled it."""

    __tablename__ = "process_commands"
    __table_args__ = (
        # Partial: the reaper scans only open rows.
        Index(
            "ix_process_commands_open",
            "issued_at",
            postgresql_where=text("status IN ('pending_publish', 'in_flight')"),
        ),
        Index("ix_process_commands_fetch_command_id", "fetch_command_id"),
        # Serves the reaper's max(fact_at), which dates the processor's latest
        # answer in its held-commands warning. The decision itself reads
        # published_at (CR 1) and has no index of its own yet (CR 11).
        Index("ix_process_commands_fact_at", "fact_at"),
    )

    command_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    fetch_command_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("fetch_commands.command_id", ondelete="CASCADE"),
        nullable=False,
    )
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("watched_items.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- the command, snapshotted so the sweep can republish from the row ---
    info_source_id: Mapped[str] = mapped_column(String(26), nullable=False)
    input_uri: Mapped[str] = mapped_column(Text, nullable=False)
    input_digest: Mapped[str] = mapped_column(Text, nullable=False)
    spec_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The resolved dispatch essence; NULL is a real resolution (HTML fallback).
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ProcessCommandStatus.PENDING_PUBLISH
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    reissue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- fact fields (written by the content.derived consumer) ---
    # The latest fact's ``occurred_at`` — the processor's clock, transient
    # facts included. Set means "answered": the reaper re-issues a stale command
    # only once a command published after it has one (CR 1). Never written by
    # Watcher itself, so it always says when the processor last spoke.
    fact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    empty: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    output_digest: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    output_uri: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    output_media_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    spec_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    spec_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    processor_version: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # --- shadow mode: local extraction's answer, and the comparator's verdict ---
    local_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    # NULL exactly when local extraction failed.
    local_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    local_spec_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    shadow_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    shadow_detail: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
