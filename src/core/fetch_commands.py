"""The content.fetch issue path — Watcher as Replicator's command issuer (#241).

Since the Phase-4 cutover this is the only path by which Watcher obtains
content: it makes no origin request of its own.

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
  replay a no-op. Since cannobserv#300 this is bookkeeping rather than
  correctness (the wire carries the domain key) — but the row still holds what
  the wire does not, so nothing about the order changes.
* **replicator#11** — the command pins watcher's User-Agent so the cutover is
  UA-neutral and fingerprints stay byte-continuous.
* **cannobserv#300** — every command names its ``info_source_id``, snapshotted
  onto the row at issue time so the sweep can republish without a WatchedItem.

* **#269** — the command replays the item's stored conditional-GET validators
  (``If-None-Match`` / ``If-Modified-Since``), **snapshotted onto the row at
  issue** rather than read from the item at publish: the sweep holds only the
  row. Which occasions may replay, and which must re-fetch in full, is
  ``src/core/validators.py``; the gate is off until an item is named in
  ``WATCHER_CONDITIONAL_GET_ENABLED``. Safe only because replicator#17 (the
  ``not_modified`` outcome) and #249 part 1 (Watcher's handling of it) are both
  in production — a validator sent to a deployment that still classifies 304 as
  a plain fetch failure reproduces the trap #249 closed.
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

from src.core.logging import get_logger
from src.core.models.fetch_command import OPEN_STATUSES, FetchCommand, FetchCommandStatus
from src.core.models.watched_item import WatchedItem
from src.core.validators import replayable_validators

logger = get_logger(__name__)

# Watcher's User-Agent, sent on every command's ``headers`` (replicator#11).
# **Load-bearing for change detection:** fingerprints are UA-sensitive, so this
# value must keep matching what the pre-cutover inline fetcher sent, or every
# watched item reports a spurious change on its next check. Do not "tidy" it.
WATCHER_USER_AGENT = "watcher/0.1.0"

FETCH_COMMAND_TIMEOUT_ENV = "WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS"
DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS = 1800.0


def _command_headers(row: FetchCommand) -> dict[str, str]:
    """The command's ``headers`` map: the pinned UA, plus any snapshotted validators.

    Lower-cased names, one line each. Replicator folds case before merging, but
    "issuer wins" is only a rule if exactly one field line goes on the wire.
    Values are replayed **verbatim and unparsed** — ``W/`` prefix, quotes, and
    the origin's own date spelling included; the send-side guard at snapshot
    time already refused anything Replicator would not send.
    """
    headers = {"user-agent": WATCHER_USER_AGENT}
    if row.request_etag:
        headers["if-none-match"] = row.request_etag
    if row.request_last_modified:
        headers["if-modified-since"] = row.request_last_modified
    return headers


async def create_fetch_command(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    now: datetime,
    intent_id: str | None = None,
    reissue_count: int = 0,
    force_full_fetch: bool = False,
) -> FetchCommand:
    """Persist the pending command row (MUST-2: caller commits before publishing).

    ``intent_id`` defaults to a fresh ULID (a new intent); the reaper passes the
    prior command's ``intent_id`` on re-issue so lineage survives the id change.

    The conditional-GET validators are resolved **here**, at the occasion, and
    stored on the row (#269): the pending-publish sweep republishes from the row
    alone, so a value re-read at publish time could differ from what the command
    was minted to ask. ``force_full_fetch`` is the operator's "check now" — an
    unconditional re-read, whatever is stored.
    """
    request_etag, request_last_modified = replayable_validators(
        watched_item, now=now, force_full_fetch=force_full_fetch
    )
    row = FetchCommand(
        command_id=str(ULID()),  # MUST-1: per occasion, never per resource
        intent_id=intent_id if intent_id is not None else str(ULID()),
        watched_item_id=watched_item.id,
        url=watched_item.effective_url,
        # Snapshotted at the occasion so the sweep — which holds no WatchedItem
        # — can publish a valid command (cannobserv#300; NOT NULL since #251).
        info_source_id=watched_item.archiver_info_source_id,
        status=FetchCommandStatus.PENDING_PUBLISH,
        issued_at=now,
        reissue_count=reissue_count,
        request_etag=request_etag,
        request_last_modified=request_last_modified,
        forced_full_fetch=force_full_fetch,
    )
    session.add(row)
    return row


async def publish_fetch_command(
    client: Redis, row: FetchCommand, *, now: datetime | None = None
) -> None:
    """XADD the command and mark the row in-flight (caller commits).

    Publishes through ``to_wire`` over the strict Emit class — never hand-rolled
    fields (a hand-built frame dead-letters silently on the consumer side). The
    pinned User-Agent is the replicator#11 byte-continuity guarantee; the
    validators, when present, come off the row (#269) so the sweep's republish
    is byte-identical to the original command.
    """
    command = ContentFetchCommandEmit(
        occurred_at=row.issued_at,
        command_id=row.command_id,
        url=row.url,
        info_source_id=row.info_source_id,
        headers=_command_headers(row),
    )
    await AsyncBusPublisher(client).execute(BusPublish(streams.CONTENT_FETCH, to_wire(command)))
    row.status = FetchCommandStatus.IN_FLIGHT
    row.published_at = now if now is not None else datetime.now(UTC)


async def get_open_command(session: AsyncSession, watched_item_id) -> FetchCommand | None:
    """The item's open command (awaiting publish or a fact), if any.

    The scheduling gate reads this: without it, a silently failed command
    re-issues every ``schedule_tick`` — 1,440 real origin fetches a day for one
    404ing item. Returns the row rather than a bool so callers can say *how
    long* it has been open (the check-now rejection does).
    """
    stmt = (
        select(FetchCommand)
        .where(
            FetchCommand.watched_item_id == watched_item_id,
            FetchCommand.status.in_(OPEN_STATUSES),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_open_command(session: AsyncSession, watched_item_id) -> bool:
    """True when the item already has a command awaiting publish or a fact."""
    return await get_open_command(session, watched_item_id) is not None


def fetch_command_timeout_seconds() -> float:
    """How long an in-flight command may go without a signal before the reaper
    expires and re-issues it.

    Deliberately generous (default 1800): Replicator's reclaim cadence is an
    operator knob on another host, and a tight value re-issues under live
    retries. Read here so the reaper and the check-now rejection quote the same
    number.
    """
    return float(
        os.environ.get(FETCH_COMMAND_TIMEOUT_ENV, str(DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS))
    )


async def select_pending_publish(session: AsyncSession, *, limit: int = 100) -> list[FetchCommand]:
    """Rows committed but never confirmed on the bus — the sweep's work list."""
    stmt = (
        select(FetchCommand)
        .where(FetchCommand.status == FetchCommandStatus.PENDING_PUBLISH)
        .order_by(FetchCommand.issued_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
