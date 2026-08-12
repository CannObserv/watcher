"""Definitive schedule-display helper (#206).

A single source of truth for "what interval does this WatchedItem resolve to, is
it inherited, is a temporal profile currently overriding it, and when is its next
check?" — composed from the same primitives the scheduler uses so the UI cannot
drift from ``schedule_tick``:

- ``resolved_schedule_config`` — the 4-tier base chain under the throttle floor
  (registry → item → domain → system; #205, #254)
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
from src.core.scheduling.resolution import base_schedule_config, resolved_schedule_config

ScheduleSource = Literal["registry", "item", "domain", "default"]


@dataclass(frozen=True)
class ScheduleDisplay:
    """Resolved schedule facts for one WatchedItem, ready to render.

    ``interval_text`` is the cadence to show — the profile cadence when
    ``profile_active`` is True, otherwise the base 4-tier resolution *under the
    throttle floor*, i.e. what the scheduler will actually use. ``source`` is
    where the base interval came from (``registry``/``item``/``domain``/``default``).
    ``throttled`` is True when the ``reduce_frequency`` floor is what set the text
    rather than the tier. ``next_check`` is ``None`` only when the item has never
    been checked.
    """

    interval_text: str
    source: ScheduleSource
    profile_active: bool
    next_check: datetime | None
    throttled: bool = False

    @property
    def marker(self) -> str | None:
        """Source word rendered after a "·" in the UI, or ``None`` for an explicit
        item interval.

        Precedence is by what is actually in force: ``"profile"`` when a temporal
        profile is currently overriding the base cadence, then ``"throttled"``
        when the floor is (#254 — without this a throttle is invisible, since it
        no longer writes the item config it used to show up in), then
        ``"registry"``/``"domain"``/``"default"`` for a non-local tier.
        """
        if self.profile_active:
            return "profile"
        if self.throttled:
            return "throttled"
        return None if self.source == "item" else self.source


def _base_source(watched_item: WatchedItem) -> ScheduleSource:
    """Which tier the base interval came from — mirrors ``base_schedule_config``."""
    if watched_item.announced_schedule_config is not None:
        return "registry"
    if watched_item.default_schedule_config is not None:
        return "item"
    if watched_item.domain_default_schedule_config is not None:
        return "domain"
    return "default"


def _base_interval(watched_item: WatchedItem, resolved: dict) -> tuple[str, ScheduleSource, bool]:
    """Render ``(interval_text, source, throttled)`` from an already-resolved config.

    Takes ``resolved`` rather than recomputing it (#254 CR-9): the caller needs
    the same config for ``compute_next_check``, and resolving three times per row
    on a list view is work for nothing.

    The text comes from ``resolved_schedule_config`` — the 4-tier chain *under
    the throttle floor* — so the UI cannot show a cadence the scheduler will not
    use. ``source`` still names the tier the base came from, and ``throttled``
    says whether the floor is what moved the number (#254): a throttle used to be
    visible because it wrote the item config, and reporting it here is what keeps
    it visible now that it does not.

    An intervalless config (an explicit ``{}`` at any tier) shows the literal
    ``"{ }"`` rather than a blank beside an inherited tag (#202 CR).
    """
    source = _base_source(watched_item)
    interval = resolved.get("interval")
    base_interval = base_schedule_config(watched_item).get("interval")
    throttled = interval is not None and interval != base_interval
    return (interval if interval else "{ }", source, throttled)


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
    resolved = resolved_schedule_config(watched_item)
    base_text, source, throttled = _base_interval(watched_item, resolved)

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
            resolved,
            watched_item.last_checked_at,
            now=now,
            profile_interval=profile_interval,
        )

    return ScheduleDisplay(
        interval_text=interval_text,
        source=source,
        profile_active=profile_active,
        next_check=next_check,
        throttled=throttled,
    )
