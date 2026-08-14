"""The info.watch-status producer — the return leg of the registry channel (#264).

Archiver announces desired state on ``info.registry``; Watcher reports what it
has *actually applied* here, as ``WatchStatusState`` frames keyed
``info_item_id`` (contract: cannobserv#321; consumer: archiver#151, which
renders the watched-item panel and the announced-vs-applied drift detector
from this stream with zero SDK calls). Same posture as ``content.fetch-policy``:
config/state kind, broadcast LWW, no consumer group, no DLQ, periodic full
republish, retention riding on the producer's ``maxlen``.

**Every publish is built from committed rows.** The contract's one ordering
rule — publish ``applied_generation`` only after the reconcile commits, or the
drift detector lies in the one direction that matters — holds here by
construction rather than by call-site discipline: mutation paths defer a
republish and the deferred task reads the database, so there is no in-flight
value to leak.

**Levels, not edges.** Every field answers "what is true now", never "how many
times", so messages coalesce without loss and the publish rate scales with
*mutation* rate, not fetch activity. Mutation paths (reconcile, health
transition, active/cadence change) defer a republish; a steadily-healthy item
publishes once per periodic tick however often it is fetched. Under-reporting
is the safe direction — the registry must never claim content is fresher than
it is.

**No outbox, loss accepted explicitly.** Unlike a registry mutation, a dropped
frame here costs nothing durable: the next full republish corrects it, so the
republish period is the recovery bound (`src/workers/watch_status.py`).

Field mappings the consumer cannot check for us:

* ``applied_generation`` — the row's value, or **0** for a never-reconciled
  row. The sentinel means "nothing newer than generation 0 applied": a
  never-*mutated* InfoItem legitimately announces at 0 (archiver#141 bumps
  only on mutation; its snapshot emits live entries at their raw generation),
  so 0 collapses "never reconciled" and "reconciled at 0" — benign under
  apply-iff-greater, and the drift detector's clean read at 0==0 is at worst
  transiently optimistic: the consumer replays the registry from ``0-0`` at
  every boot, and the first real mutation bumps to ``>= 1`` and drifts
  loudly if unapplied.
* ``applied_active`` — the conjunction the scheduler actually gates on
  (``is_active`` AND un-archived AND domain not suspended), never a bare echo
  of the announcement.
* ``applied_interval`` — the concrete resolved cadence **after** the throttle
  floor, so cadence-only divergence (unparseable spec, delegation, floor) is
  visible while ``applied_active`` never moves, and Archiver's derived
  ``next_due_at`` stays truthful.
* ``health`` — ``"ok"`` / ``"error"`` / ``"unknown"`` (#328); both
  pre-first-check states (UNKNOWN, PROBING) map to ``"unknown"`` because the
  probe/first-fact distinction is producer mechanism, not registry state.
* Tombstones ride ``revoked_info_items`` into every full set. A never-
  reconciled item deleted locally gets **no tombstone** — it was never
  announced, so Archiver holds no entry to retire (and the DELETE route 409s
  on reconciled items, so that case cannot arise).
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import to_wire
from co_core.pure.models.changes import WatchStatusEmit
from co_core_aio.bus import AsyncBusPublisher
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.core.models.revoked_info_item import RevokedInfoItem
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.scheduling.resolution import resolved_schedule_config

logger = get_logger(__name__)

_HEALTH_WIRE = {
    WatchHealthStatus.OK: "ok",
    WatchHealthStatus.ERROR: "error",
    WatchHealthStatus.UNKNOWN: "unknown",
    WatchHealthStatus.PROBING: "unknown",
}


def _health_wire_value(status: WatchHealthStatus | None) -> str:
    # The column predates its NOT NULL intent; a NULL row is "never checked".
    return _HEALTH_WIRE.get(status, "unknown")


def build_status_events(
    items: Sequence[WatchedItem],
    tombstones: Sequence[RevokedInfoItem],
    *,
    now: datetime,
) -> list[WatchStatusEmit]:
    """One ``WatchStatusEmit`` per WatchedItem (live) and per tombstone (revoked).

    A row the model rejects is skipped with a warning rather than failing the
    batch — one unpublishable row must not stop the rest of the corpus's
    statuses from travelling.
    """
    events: list[WatchStatusEmit] = []
    for item in items:
        try:
            events.append(
                WatchStatusEmit(
                    occurred_at=now,
                    info_item_id=str(item.archiver_info_item_id),
                    applied_generation=(
                        item.applied_generation if item.applied_generation is not None else 0
                    ),
                    applied_active=(
                        item.is_active and item.archived_at is None and not item.domain_suspended
                    ),
                    health=_health_wire_value(item.health_status),
                    applied_interval=resolved_schedule_config(item).get("interval"),
                    last_attempt_at=item.last_checked_at,
                    last_observed_at=item.last_observed_at,
                )
            )
        except ValidationError:
            logger.warning(
                "skipping unpublishable watch-status row",
                extra={"info_item_id": str(item.archiver_info_item_id), "kind": "live"},
            )
    for tombstone in tombstones:
        try:
            events.append(
                WatchStatusEmit(
                    occurred_at=now,
                    info_item_id=tombstone.info_item_id,
                    applied_generation=tombstone.generation,
                    revoked=True,
                )
            )
        except ValidationError:
            logger.warning(
                "skipping unpublishable watch-status row",
                extra={"info_item_id": tombstone.info_item_id, "kind": "tombstone"},
            )
    return events


async def publish_status_events(client: Redis, events: Sequence[WatchStatusEmit]) -> int:
    """XADD each event to ``info.watch-status``; returns the count published."""
    publisher = AsyncBusPublisher(client)
    for event in events:
        await publisher.execute(BusPublish(streams.INFO_WATCH_STATUS, to_wire(event)))
    return len(events)


async def publish_full_status_set(session: AsyncSession, client: Redis) -> int:
    """Publish the whole status set: every WatchedItem, plus every tombstone.

    Archived and paused items still publish — ``applied_active=False`` *is*
    their status, and a frame for an item nobody schedules is harmless where a
    missing one leaves the panel stale.
    """
    items = (await session.execute(select(WatchedItem))).scalars().all()
    tombstones = (await session.execute(select(RevokedInfoItem))).scalars().all()
    events = build_status_events(items, tombstones, now=datetime.now(UTC))
    return await publish_status_events(client, events)
