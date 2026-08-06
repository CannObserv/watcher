"""The content.fetch issue path — Watcher as Replicator's command issuer (#241).

Phase 4 step 1: everything here is inert until ``WATCHER_FETCH_MODE=bus``.

The contract this implements is normative in the Replicator repo
(``docs/contracts/content-fetch-issuer-contract.md``); the MUSTs that land in
this module:

* **MUST-1** — ``mint_id`` per fetch *occasion*: a fresh ULID per call, never
  derived from the item or URL (a resource-stable id makes every re-fetch
  inside Replicator's 24 h dedupe TTL a silent no-op).
* **MUST-2** — persist-before-publish: ``create_fetch_command`` writes the
  ``command_id → WatchedItem`` row for the caller to **commit before** any
  XADD. A crash between commit and publish leaves ``pending_publish``, which
  the sweep republishes under the *same* id — Replicator's dedupe makes the
  replay a no-op.
* **replicator#11** — the command pins watcher's User-Agent so the cutover is
  UA-neutral and fingerprints stay byte-continuous.

No validator headers (``If-None-Match``/``If-Modified-Since``) are sent: a
body-less 304 still dead-letters (replicator#17). Revisit when that closes.
"""

import os
from datetime import UTC, datetime

from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import to_wire
from co_core.pure.models.changes import ContentFetchCommandEmit
from co_core_aio.bus import AsyncBusPublisher
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.fetch import WATCHER_USER_AGENT
from src.core.logging import get_logger
from src.core.models.fetch_command import OPEN_STATUSES, FetchCommand, FetchCommandStatus
from src.core.models.watched_item import WatchedItem

logger = get_logger(__name__)

# local (default) = today's inline fetch; bus = issue content.fetch commands.
# Read at issue time so the flag flips without a restart-ordering dance.
FETCH_MODE_ENV = "WATCHER_FETCH_MODE"


def fetch_mode() -> str:
    """``"bus"`` when explicitly opted in; anything else resolves to ``"local"``.

    Fail-safe direction: an unrecognized value keeps today's proven path and
    warns, rather than routing fetches onto a bus nobody meant to enable.
    """
    value = os.environ.get(FETCH_MODE_ENV, "local")
    if value in ("local", "bus"):
        return value
    logger.warning(
        "unrecognized WATCHER_FETCH_MODE %r — falling back to 'local'",
        value,
    )
    return "local"


async def create_fetch_command(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    now: datetime,
    intent_id: str | None = None,
    reissue_count: int = 0,
) -> FetchCommand:
    """Persist the pending command row (MUST-2: caller commits before publishing).

    ``intent_id`` defaults to a fresh ULID (a new intent); the reaper passes the
    prior command's ``intent_id`` on re-issue so lineage survives the id change.
    """
    row = FetchCommand(
        command_id=str(ULID()),  # MUST-1: per occasion, never per resource
        intent_id=intent_id if intent_id is not None else str(ULID()),
        watched_item_id=watched_item.id,
        url=watched_item.effective_url,
        status=FetchCommandStatus.PENDING_PUBLISH,
        issued_at=now,
        reissue_count=reissue_count,
    )
    session.add(row)
    return row


async def publish_fetch_command(
    client: Redis, row: FetchCommand, *, now: datetime | None = None
) -> None:
    """XADD the command and mark the row in-flight (caller commits).

    Publishes through ``to_wire`` over the strict Emit class — never hand-rolled
    fields (a hand-built frame dead-letters silently on the consumer side). The
    pinned User-Agent is the replicator#11 byte-continuity guarantee.
    """
    command = ContentFetchCommandEmit(
        occurred_at=row.issued_at,
        command_id=row.command_id,
        url=row.url,
        headers={"user-agent": WATCHER_USER_AGENT},
    )
    await AsyncBusPublisher(client).execute(BusPublish(streams.CONTENT_FETCH, to_wire(command)))
    row.status = FetchCommandStatus.IN_FLIGHT
    row.published_at = now if now is not None else datetime.now(UTC)


async def has_open_command(session: AsyncSession, watched_item_id) -> bool:
    """True when the item already has a command awaiting publish or a fact.

    The scheduling gate: without it, a silently failed command re-issues every
    ``schedule_tick`` — 1,440 real origin fetches a day for one 404ing item.
    """
    stmt = (
        select(FetchCommand.command_id)
        .where(
            FetchCommand.watched_item_id == watched_item_id,
            FetchCommand.status.in_(OPEN_STATUSES),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def select_pending_publish(session: AsyncSession, *, limit: int = 100) -> list[FetchCommand]:
    """Rows committed but never confirmed on the bus — the sweep's work list."""
    stmt = (
        select(FetchCommand)
        .where(FetchCommand.status == FetchCommandStatus.PENDING_PUBLISH)
        .order_by(FetchCommand.issued_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
