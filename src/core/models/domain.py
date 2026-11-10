"""Domain model — one row per host, carrying the break-glass suspension flag."""

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class Domain(Base, TimestampMixin):
    """A host Watcher has an item on, and whether Watcher may check it.

    ``is_active`` is the break-glass suspension (#372): deactivating a domain
    suspends every WatchedItem on it through the denormalized
    ``WatchedItem.domain_suspended``. It is Watcher's, not the registry's.

    The rest went to Archiver with the headless jump (#368) and was dropped in
    #380: ``min_interval`` (the politeness floor, Archiver's since F1 #374),
    ``default_schedule_config`` (the domain cadence tier, since F2 #375),
    ``notes`` and ``archived_at`` (the operator surface, #377/#378). The
    in-process limiter's columns went earlier (#241 step 5, #272).
    """

    __tablename__ = "domains"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults."""
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)

    @property
    def is_suspended(self) -> bool:
        """Deactivated: the one state that stops Watcher watching the host.

        One spelling of the predicate (#250, CR-1 finding 7): it decides
        ``WatchedItem.domain_suspended`` when an item is created or moves host.
        Until #380 an archived domain counted too; nothing archives one now.
        """
        return not self.is_active
