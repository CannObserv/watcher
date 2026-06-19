"""Notification template model — one scoped table for all dispatch sources (#200).

Post-#200 the five legacy dispatch sources (global flag, ``domain_nc_refs``,
``watch_nc_refs``, ``watched_item_notification_templates``,
``watch_notification_configs``) collapse into this single table. Each row is a
Notification Template with an intrinsic ``visibility`` that controls where it
fires:

* ``global`` — fires for every WatchedItem (``domain_name``/``watched_item_id``
  both NULL).
* ``domain`` — fires for every WatchedItem in ``domain_name``.
* ``watched_item`` — fires for the single ``watched_item_id`` only.

A CHECK constraint enforces that exactly the columns implied by ``visibility``
are populated. There is no separate "configuration" object — the channel
(``remote_channel_id``) and scope are columns on the template row.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

VISIBILITY_GLOBAL = "global"
VISIBILITY_DOMAIN = "domain"
VISIBILITY_WATCHED_ITEM = "watched_item"
VISIBILITIES = (VISIBILITY_GLOBAL, VISIBILITY_DOMAIN, VISIBILITY_WATCHED_ITEM)


class NotificationTemplate(TimestampMixin, Base):
    """A notification target with an intrinsic visibility scope (#200)."""

    __tablename__ = "notification_templates"
    __table_args__ = (
        CheckConstraint(
            "(visibility = 'global' AND domain_name IS NULL AND watched_item_id IS NULL) OR "
            "(visibility = 'domain' AND domain_name IS NOT NULL AND watched_item_id IS NULL) OR "
            "(visibility = 'watched_item' AND watched_item_id IS NOT NULL "
            "AND domain_name IS NULL)",
            name="ck_notification_templates_visibility_refs",
        ),
    )

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_hint: Mapped[str] = mapped_column(String(50), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        server_default=text("ARRAY['change_detected']::varchar[]"),
    )
    # Visibility scope: which WatchedItems this template fires for. See module docstring.
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=VISIBILITY_GLOBAL
    )
    # Set iff visibility='domain'; SQL NULL otherwise (enforced by the CHECK constraint).
    domain_name: Mapped[str | None] = mapped_column(
        String(253), ForeignKey("domains.name", ondelete="CASCADE"), nullable=True, index=True
    )
    # Set iff visibility='watched_item'; SQL NULL otherwise (enforced by the CHECK constraint).
    watched_item_id: Mapped[ULID | None] = mapped_column(
        ULIDType, ForeignKey("watched_items.id", ondelete="CASCADE"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # none_as_null=True: None persists as SQL NULL, not JSONB 'null' (#198).
    content_config: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True, default=None
    )
    # The notifier-service channel ULID — the real delivery target. ``channel_hint`` is a
    # human-readable scheme tag ("slack", "mailto") for display only; nothing dispatches off it.
    remote_channel_id: Mapped[str | None] = mapped_column(String(26), nullable=True, default=None)
