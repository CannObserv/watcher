"""Domain model — per-domain rate limiter configuration."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_DECAY_WINDOW = 1800.0


class Domain(Base, TimestampMixin):
    """Per-domain rate limiter configuration and backoff state."""

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

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults."""
        kwargs.setdefault("min_interval", DEFAULT_MIN_INTERVAL)
        kwargs.setdefault("max_concurrency", DEFAULT_MAX_CONCURRENCY)
        kwargs.setdefault("current_interval", kwargs.get("min_interval", DEFAULT_MIN_INTERVAL))
        kwargs.setdefault("decay_window", DEFAULT_DECAY_WINDOW)
        super().__init__(**kwargs)
