"""FetchPolicyTombstone — hosts whose fetch policy has been revoked (#245).

The ``content.fetch-policy`` stream is last-write-wins with no delete: revoking
a host's policy means publishing a ``revoked=True`` tombstone, and the contract
(cannobserv#285) requires tombstoned hosts to *keep appearing* in the periodic
full-set republish — otherwise broker trimming eventually ages the tombstone out
and a booting consumer replays a stale live value it can no longer revoke. A
deleted ``Domain`` row cannot carry that obligation, so the #209 delete path
records the host here and the producer merges this table into every republish.

Rows are removed only when a Domain with the same name is re-created (the host
is live again). Bounded by the count of deleted domains — effectively tiny.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base, TimestampMixin


class FetchPolicyTombstone(Base, TimestampMixin):
    """A host whose fetch policy is revoked, republished until the host returns."""

    __tablename__ = "fetch_policy_tombstones"

    host: Mapped[str] = mapped_column(String(253), primary_key=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
