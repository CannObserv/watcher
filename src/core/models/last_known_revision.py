"""Watcher-local cache of the most recent SourceRevision per InfoSource.

Used by the pipeline fast-path to skip POST when the freshly-computed
fingerprint matches the previous one. Eliminates the need for an
`list_source_revisions` SDK method.

Keyed by info_source_id (primary key, not the row's ULID) — there's
exactly one row per source.
"""

from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType


class LastKnownRevision(Base):
    """Watcher-local fingerprint cache, one row per info_source_id."""

    __tablename__ = "last_known_revisions"

    info_source_id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True)
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision_id: Mapped[ULID] = mapped_column(ULIDType, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
