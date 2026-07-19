"""Definitive schedule-display helper (#206).

A single source of truth for "what interval does this WatchedItem resolve to, is
it inherited, is a temporal profile currently overriding it, and when is its next
check?" — composed from the same primitives the scheduler uses so the UI cannot
drift from ``schedule_tick``:

- ``resolved_schedule_config`` — the 3-tier base chain (item → domain → system; #205)
- ``resolve_effective_interval`` — the temporal-profile override (#204 CR finding 2)
- ``compute_next_check`` — the next-due datetime

Every surface that renders a resolved interval / next-check (list view, detail
page, domain-detail table) goes through :func:`resolve_schedule_display`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.core.models.watched_item import WatchedItem
from src.core.scheduling.cadence import (
    compute_next_check,
    format_interval,
    resolve_effective_interval,
)
from src.core.scheduling.resolution import resolved_schedule_config

ScheduleSource = Literal["item", "domain", "default"]


@dataclass(frozen=True)
class ScheduleDisplay:
    """Resolved schedule facts for one WatchedItem, ready to render.

    ``interval_text`` is the cadence to show — the profile cadence when
    ``profile_active`` is True, otherwise the base 3-tier resolution. ``source`` is
    where the *base* interval came from (``item``/``domain``/``default``).
    ``next_check`` is ``None`` only when the item has never been checked.
    """

    interval_text: str
    source: ScheduleSource
    profile_active: bool
    next_check: datetime | None

    @property
    def marker(self) -> str | None:
        """Source word rendered after a "·" in the UI, or ``None`` for an explicit
        item interval. ``"profile"`` when a temporal profile is currently overriding
        the base cadence; otherwise ``"domain"``/``"default"`` for an inherited tier.
        """
        if self.profile_active:
            return "profile"
        return None if self.source == "item" else self.source


def _base_interval(watched_item: WatchedItem) -> tuple[str, ScheduleSource]:
    """Resolve ``(interval_text, source)`` from the 3-tier chain, no profile.

    Mirrors the precedence in ``resolved_schedule_config``: a non-``None`` item
    config wins at its tier (an intervalless ``{}`` shows the literal ``"{ }"``
    rather than a blank beside an inherited tag — #202 CR); else a denormalized
    domain default; else the system default.
    """
    item_cfg = watched_item.default_schedule_config
    if item_cfg is not None:
        interval = item_cfg.get("interval")
        return (interval, "item") if interval else ("{ }", "item")
    source: ScheduleSource = (
        "domain" if watched_item.domain_default_schedule_config is not None else "default"
    )
    # An intervalless inherited config (a stray domain ``{}``) shows the literal
    # rather than a blank beside the marker — symmetric with the item tier. The
    # write boundary rejects empty domain defaults, so this is defensive only.
    interval = resolved_schedule_config(watched_item).get("interval")
    return (interval, source) if interval else ("{ }", source)


def resolve_schedule_display(
    watched_item: WatchedItem,
    *,
    now: datetime,
    profiles: list[dict] | None = None,
) -> ScheduleDisplay:
    """Resolve the display facts for ``watched_item`` at ``now``.

    Pass ``profiles`` (the item's active TemporalProfile rows as dicts, the same
    shape ``schedule_tick`` builds) to honor a currently-active profile override;
    omit it for a base-cadence-only view. When a profile is currently shortening
    the cadence, ``interval_text`` shows the profile interval and ``profile_active``
    is True — the tier ``source`` still reflects where the base came from.
    """
    base_text, source = _base_interval(watched_item)

    profile_interval = None
    if profiles:
        profile_interval = resolve_effective_interval(profiles, today=now.date())
    profile_active = profile_interval is not None

    interval_text = format_interval(profile_interval) if profile_active else base_text

    if watched_item.last_checked_at is None:
        next_check = None
    else:
        # Reuse the profile interval already resolved above — no second
        # resolve_effective_interval pass inside compute_next_check.
        next_check = compute_next_check(
            resolved_schedule_config(watched_item),
            watched_item.last_checked_at,
            now=now,
            profile_interval=profile_interval,
        )

    return ScheduleDisplay(
        interval_text=interval_text,
        source=source,
        profile_active=profile_active,
        next_check=next_check,
    )
