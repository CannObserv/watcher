"""RevokedInfoItem — InfoItems the registry has retired, and the generation that did it (#254).

``info.registry`` is last-write-wins with no delete: retiring an InfoItem means
publishing a ``revoked=True`` tombstone, and Watcher answers by deleting the
WatchedItem. That deletion takes ``applied_generation`` with it, which is a
problem, because the whole reason ``generation`` exists is that **the producer's
outbox drain reorders under retry** — a transiently-failed row is skipped and
published on a later drain.

So the stale-announcement case the ordering guard is built for is exactly the one
the delete disarms. Concretely: generation 5 announces the item live, generation 6
revokes it, and the two arrive in the order 6 then 5. Without this table the
gen-5 announcement lands on no row, has nothing to compare ``>`` against, and
**resurrects an item the registry has retired**. Boot replay from ``0-0`` is a
milder version of the same shape — every live announcement preceding the
tombstone is re-seen.

Recording the revoking generation here keeps the comparison total: every key has
a left-hand side, whether or not it still has a row. Mirrors
``FetchPolicyTombstone``, which solves the same problem one stream over.

Rows are removed when a *newer* generation announces the key live again — a
genuine un-revoke, which the contract permits since ``revoked`` is a level and
not an event. Bounded by the count of retired InfoItems.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base, TimestampMixin


class RevokedInfoItem(Base, TimestampMixin):
    """A retired InfoItem's id and the announcement generation that retired it."""

    __tablename__ = "revoked_info_items"

    # String(26), matching `watched_items.archiver_info_source_id` and the wire
    # form: this is a foreign registry's ULID, so it is stored as the producer
    # spells it rather than round-tripped through our own ULID type.
    info_item_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
