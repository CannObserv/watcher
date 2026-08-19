"""FetchCommand — outbox, pending map, and inbox for content.fetch (#241, Phase 4).

One row per issued ``ContentFetchCommand``, keyed by the ``command_id`` ULID —
the correlator the bus carries (MUST-3: ``url`` is one-to-many against
InfoSources and is never a key). The row is written and committed *before* the
XADD (persist-before-publish); the fact fields are upserted by the
``content.blobs`` consumer (MUST-4: at-least-once, per-emission keys — several
distinct facts per command are normal, last terminal wins).

**MUST-2 is bookkeeping now, not correctness** (cannobserv#300, #252). Until the
domain key rode on the wire, losing this row made the returning fact permanently
uncorrelatable; ``info_source_id`` on all three content contracts makes the fact
self-describing, so the row's remaining job is what the wire does *not* carry —
request options, health bookkeeping, re-issue lineage, and the reaper's state.
Watcher still correlates on ``command_id`` alone: a fact naming one of our
InfoSources may answer another issuer's command on the broadcast stream.

``intent_id`` is lineage: one fetch *intent* may span several ``command_id``s
when the reaper re-issues after a silent failure (MUST-6 — a timeout means
re-issue under a fresh id, never conclude failure).

``content_fingerprint`` here is Replicator's **raw-bytes** identity — distinct
from watcher's extracted-text fingerprint on ``ChangeRevision``; the two coexist
(MUST-5: never dedupe correlation on it).
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
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType


class FetchCommandStatus(enum.StrEnum):
    """Lifecycle of one issued command."""

    PENDING_PUBLISH = "pending_publish"  # row committed, XADD not yet confirmed
    IN_FLIGHT = "in_flight"  # published; awaiting a fact
    SUCCEEDED = "succeeded"  # blob applied through the pipeline
    NOT_MODIFIED = "not_modified"  # origin answered 304; closed, no blob (#249)
    FAILED = "failed"  # terminal fetch_failed (or re-issue cap hit)
    SUPERSEDED = "superseded"  # a newer command for the item applied first
    EXPIRED = "expired"  # reaped after timeout; re-issued under a fresh id


# The one ``FetchFailedEvent.reason`` that is **not** a failure (co-core 0.10.0,
# CannObserv/replicator#17): a conditional request whose validator matched, so
# the origin answered 304 and there is no body. It rides ``fetch_failed`` because
# the event's real meaning is "this command will not produce a blob" — co-core's
# own registry rejected a dedicated ``content_unchanged`` event and wrote the
# trade-off down. Watcher's response is to close the command and keep the content
# it already has; see ``apply_fetch_not_modified`` (#249).
NOT_MODIFIED_REASON = "not_modified"

# The one ``reason`` for which no request ever went out (replicator#11): the
# command's ``headers`` or ``timeout_seconds`` were refused before the origin was
# contacted. It is the only failure that says something about *our* request
# options rather than the origin, which is why it — alone — invalidates the
# item's stored conditional-GET validators (#269).
INVALID_REQUEST_OPTIONS_REASON = "invalid_request_options"

# Watcher's own ``reason``, never Replicator's: the fact arrived and named a
# blob whose bytes could not be read (#275). Distinct from ``fetch_timeout``
# because the remedy is — the blob store, its permissions, or a backend this
# build cannot read — not an origin that stalled.
BLOB_UNREADABLE_REASON = "blob_unreadable"

# The statuses that make a command "open": they gate scheduling (no new issue
# while one is open) and are what the reaper scans.
#
# A *positive* enumeration, deliberately — a new terminal member (``NOT_MODIFIED``,
# #249) is closed by default rather than by remembering to exclude it. Keep it
# that way: the failure mode of the inverse spelling is an item whose scheduling
# gate never lifts.
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
    # The Archiver InfoSource this fetch is for, snapshotted at the occasion and
    # published on the command (cannobserv#300). Denormalized rather than joined
    # because the pending-publish sweep holds only this row — and NOT NULL so a
    # command naming no InfoSource cannot be minted at all.
    info_source_id: Mapped[str] = mapped_column(String(26), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FetchCommandStatus.PENDING_PUBLISH
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    reissue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # What this command ASKED, snapshotted at the occasion (#269) — the same
    # reason ``info_source_id`` is here: the pending-publish sweep holds only
    # this row, so a republish must be able to reproduce the exact headers.
    # Also the audit trail for a 304: what validator earned it.
    request_etag: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    request_last_modified: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Whether this occasion was asked for as an unconditional re-read — the
    # operator's check-now (#269 CR-1). Lineage, like ``intent_id``: the reaper
    # re-issues under a fresh command_id, and without carrying this the forced
    # intent is lost and the replacement may be answered 304, which is the one
    # thing check-now promises not to do.
    forced_full_fetch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # --- fact fields (upserted by the content.blobs consumer) ---
    fact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    content_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    blob_uri: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # When the blob stops being retrievable at ``blob_uri``. Echoed onward on
    # ``source_revision_observed`` under the same name (#253) — never derived
    # from the issuer contract's MUST-7 TTL, which is Replicator's policy on a
    # clock that runs from last fetch reference. NULL means unknown.
    blob_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    content_type_raw: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # The conditional-GET validators this occasion returned, verbatim (#269).
    # Provenance for the row; the pair the *next* command replays is the item's,
    # written by the apply path under its ordering guard (MUST-5).
    etag: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
