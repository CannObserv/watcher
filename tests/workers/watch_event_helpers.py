"""Proof that an ``info.watch-events`` fact rides its edge's commit (#369).

"Staged in the transaction that records the state change" is a claim about
commits, so the evidence is a log of them: for each commit, the item's health
as it committed and the facts flushed inside it. A fact staged one commit late
— or a state change committed before its fact — shows as the first commit
holding the new health with no fact in it.

Not a ``conftest.py``: imported directly, like ``bus_helpers``.
"""

from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.pending_watch_event import PendingWatchEvent
from src.core.models.watched_item import WatchedItem


@dataclass
class CommitLog:
    """One ``(health, [event_type, ...])`` entry per commit of ``session``."""

    commits: list[tuple[str | None, list[str]]] = field(default_factory=list)

    def first_commit_with(self, health) -> list[str]:
        """The facts in the first commit that left the item at ``health``."""
        for committed_health, facts in self.commits:
            if committed_health == health:
                return facts
        raise AssertionError(f"no commit left the item at {health!r}: {self.commits}")

    @property
    def facts(self) -> list[str]:
        return [kind for _health, facts in self.commits for kind in facts]


def log_commits(session: AsyncSession, item: WatchedItem) -> CommitLog:
    """Start recording ``session``'s commits as seen by ``item``."""
    log = CommitLog()
    pending: list[str] = []
    sync = session.sync_session

    @event.listens_for(sync, "after_flush")
    def _collect(flushed, _context):
        pending.extend(o.event_type for o in flushed.new if isinstance(o, PendingWatchEvent))

    @event.listens_for(sync, "after_commit")
    def _record(_committed):
        log.commits.append((item.health_status, list(pending)))
        pending.clear()

    @event.listens_for(sync, "after_soft_rollback")
    def _discard(_rolled_back, _previous):
        pending.clear()

    return log
