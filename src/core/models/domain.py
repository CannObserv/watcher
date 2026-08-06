"""Domain model — per-domain politeness floor and default check cadence."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_DECAY_WINDOW = 1800.0


class Domain(Base, TimestampMixin):
    """Per-domain politeness floor and check cadence.

    Two distinct interval concerns live here: ``min_interval`` is the
    request-level politeness floor in float seconds — since the Phase-4 cutover
    it is *published* to Replicator on ``content.fetch-policy`` (#245) rather
    than enforced in-process — while ``default_schedule_config`` is the
    operator's desired check *cadence* for items on this domain (a
    schedule_config interval string), the Domain tier of the 3-tier schedule
    resolution (#205).

    ``current_interval``, ``max_concurrency`` and ``decay_window`` are **inert**
    since #241 step 5 retired the in-process ``DomainRateLimiter``: nothing
    *reads* them for behavior any more, though creates still initialise them and
    the API still accepts and echoes them (adaptive backoff is Replicator's —
    replicator#25). ``last_request_at`` has no writer left at all. They are kept
    as columns so the retirement needed no destructive migration; the drop must
    also remove the create/PATCH write sites in ``src/api/routes/domains.py``
    and the ``DomainResponse`` fields.
    """

    __tablename__ = "domains"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    min_interval: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_MIN_INTERVAL, server_default="1.0"
    )
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_CONCURRENCY, server_default="2"
    )
    current_interval: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_MIN_INTERVAL, server_default="1.0"
    )
    last_request_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    decay_window: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_DECAY_WINDOW, server_default="1800.0"
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
        kwargs.setdefault("max_concurrency", DEFAULT_MAX_CONCURRENCY)
        kwargs.setdefault("current_interval", kwargs.get("min_interval", DEFAULT_MIN_INTERVAL))
        kwargs.setdefault("decay_window", DEFAULT_DECAY_WINDOW)
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)

    @property
    def status(self) -> str:
        """Derived status: archived > inactive > active.

        The ``backoff`` state is gone with the limiter (#241 step 5) — nothing
        raised ``current_interval`` above ``min_interval`` any more, so the
        state was unreachable and the badge always lied by omission.
        """
        if self.archived_at is not None:
            return "archived"
        if not self.is_active:
            return "inactive"
        return "active"
