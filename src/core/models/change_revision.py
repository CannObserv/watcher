"""ChangeRevision model — local fingerprint history for a WatchedItem."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class ChangeRevision(Base):
    """One fingerprint snapshot captured for a WatchedItem in a check cycle.

    Inserted on first run (baseline) and on every subsequent fingerprint
    change. Purely local: Archiver mints its own SourceRevision id on its side
    of `content.revisions` and never reports it back, so there is no registry
    id stored here (#261 dropped the retired `archiver_revision_id`). The
    mapping is re-derivable from `(info_source_id, content_fingerprint)`,
    Archiver's own uniqueness constraint.
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
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Which extraction produced the fingerprint (#324): co-core's derivation over
    # the spec the fallback loop actually bound, and the processor's version
    # string (co-core version + local generation). The diff design reads both
    # off the previous and current revisions to tell a spec- or processor-
    # induced fingerprint move from a content change. NULL on a row written
    # before this landed means *unknown* and must trigger neither label nor
    # re-baseline. `spec_fingerprint` is also NULL when co-core cannot derive
    # one (a malformed spec is a diagnostic, never a lost revision).
    spec_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    processor_version: Mapped[str | None] = mapped_column(Text, nullable=True)
