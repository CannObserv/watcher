"""Domain model — per-domain rate-limiter config and default check cadence."""

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
    """Per-domain rate-limiter configuration, backoff state, and check cadence.

    Two distinct interval concerns live here: ``min_interval``/``current_interval``
    are the request-level rate-limiter floor/backoff (float seconds), while
    ``default_schedule_config`` is the operator's desired check *cadence* for items
    on this domain (a schedule_config interval string) — the Domain tier of the
    3-tier schedule resolution (#205).
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
    # 3-tier resolution chain (#205). Distinct from min_interval/current_interval,
    # which are the request-level rate-limiter floor/backoff. none_as_null=True so
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
        """Derived status: archived > inactive > backoff > active."""
        if self.archived_at is not None:
            return "archived"
        if not self.is_active:
            return "inactive"
        if self.current_interval > self.min_interval:
            return "backoff"
        return "active"
