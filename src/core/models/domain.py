"""Domain model — per-domain politeness floor and default check cadence."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

DEFAULT_MIN_INTERVAL = 1.0


class Domain(Base, TimestampMixin):
    """Per-domain politeness floor and check cadence.

    Two distinct interval concerns live here: ``min_interval`` is the
    request-level politeness floor in float seconds — since the Phase-4 cutover
    it is *published* to Replicator on ``content.fetch-policy`` (#245) rather
    than enforced in-process — while ``default_schedule_config`` is the
    operator's desired check *cadence* for items on this domain (a
    schedule_config interval string), the Domain tier of the 3-tier schedule
    resolution (#205).

    The in-process ``DomainRateLimiter``'s columns — ``current_interval``,
    ``max_concurrency``, ``decay_window``, ``last_request_at`` — were retired
    with it (#241 step 5) and dropped in #272; adaptive backoff is Replicator's
    (replicator#25).
    """

    __tablename__ = "domains"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    min_interval: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_MIN_INTERVAL, server_default="1.0"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Operator's desired check *cadence* for items on this domain — a
    # schedule_config interval string ({"interval": "6h"}), the Domain tier of the
    # 3-tier resolution chain (#205). Distinct from min_interval, the request-level
    # politeness floor published to Replicator (#245). none_as_null=True so
    # an unset cadence persists as SQL NULL, not the JSONB 'null' literal (#198).
    default_schedule_config: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True, default=None
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults."""
        kwargs.setdefault("min_interval", DEFAULT_MIN_INTERVAL)
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)

    @property
    def is_suspended(self) -> bool:
        """Archived or deactivated — the two states that stop Watcher watching.

        One spelling of a predicate that had grown three (#250, CR-1 finding 7):
        it decides ``WatchedItem.domain_suspended`` when an item is created or a
        domain is toggled, and since #250 it also decides whether the host's
        fetch policy publishes live or ``revoked=True``. Those must never
        disagree — a host revoked on the wire but still scheduled locally, or
        the reverse, is the failure this collapses.

        Equivalent to ``status != "active"``, but kept as its own predicate:
        ``status`` is a display string whose members may grow, and a suspension
        check should not depend on which of them happen to be non-active.
        """
        return self.archived_at is not None or not self.is_active

    @property
    def status(self) -> str:
        """Derived status: archived > inactive > active.

        The ``backoff`` state is gone with the limiter (#241 step 5) — nothing
        tracked a raised interval any more, so the state was unreachable and
        the badge always lied by omission.
        """
        if self.archived_at is not None:
            return "archived"
        if not self.is_active:
            return "inactive"
        return "active"
