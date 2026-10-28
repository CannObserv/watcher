"""Definitive schedule-display helper (#206).

A single source of truth for "what interval does this WatchedItem resolve to,
where did it come from, and when is its next check?" — composed from the same
primitives the scheduler uses so the UI cannot drift from ``schedule_tick``:

- ``resolved_schedule_config`` — announced, else the system default (#375)
- ``compute_next_check`` — the next-due datetime

Every surface that renders a resolved interval / next-check (list view, detail
page, domain-detail table) goes through :func:`resolve_schedule_display`.

Before F2 (#375) the source could also be ``item`` or ``domain``, a ``throttled``
flag reported the ``reduce_frequency`` floor, and an active temporal profile
overrode the text with a ``profile`` marker. Archiver resolves all of that now
and announces the result, so none of it moves the cadence and none of it is shown.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.core.models.watched_item import WatchedItem
from src.core.scheduling.cadence import compute_next_check
from src.core.scheduling.resolution import resolved_schedule_config

ScheduleSource = Literal["registry", "default"]


@dataclass(frozen=True)
class ScheduleDisplay:
    """Resolved schedule facts for one WatchedItem, ready to render.

    ``interval_text`` is the cadence the scheduler will use; an intervalless
    config (an explicit announced ``{}``) shows the literal ``"{ }"`` rather than
    a blank beside the marker (#202 CR). ``source`` is ``registry`` when Archiver
    announced it, ``default`` for the system default. ``next_check`` is ``None``
    only when the item has never been checked.
    """

    interval_text: str
    source: ScheduleSource
    next_check: datetime | None

    @property
    def marker(self) -> str:
        """Source word rendered after a "·" in the UI."""
        return self.source


def resolve_schedule_display(watched_item: WatchedItem, *, now: datetime) -> ScheduleDisplay:
    """Resolve the display facts for ``watched_item`` at ``now``."""
    resolved = resolved_schedule_config(watched_item)
    source: ScheduleSource = (
        "registry" if watched_item.announced_schedule_config is not None else "default"
    )
    interval = resolved.get("interval")

    if watched_item.last_checked_at is None:
        next_check = None
    else:
        next_check = compute_next_check(resolved, watched_item.last_checked_at, now=now)

    return ScheduleDisplay(
        interval_text=interval if interval else "{ }",
        source=source,
        next_check=next_check,
    )
