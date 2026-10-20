"""Check-now's pre-flight: may a WatchedItem be checked now? (#371).

One verdict for both callers — the ``check-now`` route and the
``info.watch-check`` consumer — so the consumer imports nothing from
``src.api`` (#378 deletes ``/api/v1``) and the two cannot drift, the shape
#372 gave ``set_domain_active``.

Each refusal mirrors a short-circuit in ``check_watched_item`` or its issue
path, so a request that could not do anything is refused up front instead of
accepted over a silent no-op. In order, first match wins: archived, paused,
domain suspended, no URL, a fetch command already open. Each caller words the
refusal its own way: HTTP codes for the route, the closed
``CheckRejectionReason`` tokens for the consumer.
"""

import enum
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.fetch_commands import get_open_command
from src.core.models.fetch_command import FetchCommand
from src.core.models.watched_item import WatchedItem


class CheckRefusal(enum.StrEnum):
    """Why a check cannot run now."""

    ARCHIVED = "archived"
    PAUSED = "paused"  # is_active=False
    DOMAIN_SUSPENDED = "domain_suspended"
    NO_URL = "no_url"  # effective_url empty: nothing to fetch
    # The issue path's one-command gate (#241): a check is already under way.
    FETCH_OPEN = "fetch_open"


class CheckVerdict(NamedTuple):
    """``refusal`` is ``None`` when the check may run. ``open_command`` is the
    gate's row for ``FETCH_OPEN``, so a caller can say how long it has been open."""

    refusal: CheckRefusal | None
    open_command: FetchCommand | None = None


async def preflight_check(session: AsyncSession, watched_item: WatchedItem) -> CheckVerdict:
    """The verdict for checking ``watched_item`` now. Reads only; writes nothing."""
    if watched_item.archived_at is not None:
        return CheckVerdict(CheckRefusal.ARCHIVED)
    if not watched_item.is_active:
        return CheckVerdict(CheckRefusal.PAUSED)
    if watched_item.domain_suspended:
        return CheckVerdict(CheckRefusal.DOMAIN_SUSPENDED)
    if not watched_item.effective_url:
        return CheckVerdict(CheckRefusal.NO_URL)
    open_command = await get_open_command(session, watched_item.id)
    if open_command is not None:
        return CheckVerdict(CheckRefusal.FETCH_OPEN, open_command)
    return CheckVerdict(None)
