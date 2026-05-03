"""Information Item — the stable, externally-named target being tracked."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.information.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class InfoItem(Base, TimestampMixin):
    """An Information Item — one specific thing being tracked."""

    __tablename__ = "info_items"
    __table_args__ = {"schema": "information"}

    info_item_id: Mapped[ULID] = mapped_column(ULIDType(), primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
