"""The ``info.registry`` consumer — Watcher's registry inbox (#254).

Archiver announces *desired registry state* per InfoItem; Watcher makes its own
``watched_items`` accounting match. Create, spec update, cadence change, and
deactivation all fall out of one loop instead of four inbound push paths, and a
missed message self-corrects at the next snapshot rather than leaving Watcher
extracting against a stale spec permanently — which is what the old
``sync_on_spec_update`` PATCH did on any failure.

**Reconcile, do not apply.** Every rule below follows from that.

*Branch on ``revoked`` first.* A tombstone carries only ``info_item_id`` +
``generation`` + ``revoked``, so reading ``watch_spec`` before the check hits
``None`` legitimately — and collapsing paused into revoked loses the pause on the
next reconcile.

*Apply iff ``generation >`` stored.* Not ``!=``: the producer's outbox drain
reorders under retry (a transiently-failed row is skipped and published on a
later drain), so a stale announcement genuinely lands after a fresh one, and on a
last-write-wins stream it would win. A revoked key keeps its generation in
``revoked_info_items`` precisely so this comparison stays total across a delete.

*``active`` is an envelope field with four cases, and the fourth is not a state.*
``None`` means the registry has no opinion yet — keep doing what you are doing.
Treating it as ``True`` un-pauses every item an operator paused, which is exactly
what the rollout looks like before CannObserv/archiver#150's import populates the
column. On a *create* there is nothing to abstain from, so the model default
(active) stands.

*``watch_spec`` absence is a different question with a different answer.* Since
cannobserv#324 the document is required on a live announcement, so the only
delegation spelling is ``{"schema_version": 1}`` with no ``interval`` — meaning
*apply your own default*, which for Watcher is the per-domain
``default_schedule_config``. An unparseable interval resolves the same way and
**must not stop scheduling**: co-core deliberately does not validate the
document's contents, because raising at decode on a **no-DLQ** stream would drop
the message entirely and leave the LWW key stale. That tolerance has to live
here.

*Watcher owns what Archiver does not.* The announcement is authoritative for
exactly five columns — ``archiver_info_source_id``, ``effective_url``,
``source_specs``, ``announced_schedule_config``, ``is_active`` — plus
``domain_name`` and its denormalized state, and only when the host actually
moves. Everything else is Watcher's and survives: health, timings,
``domain_suspended``, ``archived_at``, ``throttle_floor_interval``,
``default_schedule_config``, media type, tags, notification config, audit rows.

*No DLQ, and no dead-letter path.* An undecodable frame is logged and skipped
with ``seek``; there is no group here, so there is no ack to skip past one with,
and a caller that forgets re-reads the same frame forever. The hour's snapshot
repairs whatever was dropped.
"""

import asyncio

from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.exceptions import BusMessageAnomaly
from co_core.pure.models.changes import RegistryAnnouncementState
from co_core_aio.bus import AsyncBusTailReader
from redis.asyncio import Redis
from sqlalchemy import select
from ulid import ULID

from src.core.domains import domain_name_for_url, ensure_domain_and_resolve_suspension
from src.core.logging import get_logger
from src.core.models.revoked_info_item import RevokedInfoItem
from src.core.models.watched_item import WatchedItem
from src.core.scheduling.cadence import parse_interval
from src.core.watched_items import derive_watched_item_name

logger = get_logger(__name__)

# Read block per poll; also the shutdown latency ceiling.
BLOCK_MS = 5000
# Insurance against a client that ignores `block` (fakeredis) busy-spinning.
IDLE_SLEEP_SECONDS = 0.05
ERROR_BACKOFF_SECONDS = 5.0
# Attempts per message before giving up on it. There is no PEL to park a message
# in and no DLQ to route it to, so the choice is retry-in-memory or drop — and a
# drop is survivable only because the periodic snapshot republishes. Bounded so a
# genuinely poisonous payload cannot wedge the loop.
MAX_APPLY_ATTEMPTS = 3

# `count=1` throughout, deliberately. `AsyncBusTailReader.read` does not advance
# its cursor unless the whole batch decodes, so a poison frame in a `count > 1`
# batch discards the well-formed messages ahead of it and needs a re-read at
# `count=1` before `seek` can pass it — reading one at a time sidesteps the
# entire sequence. The registry is small enough that the round trips are free.
READ_COUNT = 1


def _announced_schedule_config(watch_spec: dict | None) -> dict | None:
    """The announced cadence, or ``None`` when the registry delegates.

    ``None`` covers three inputs that mean the same thing to the scheduler —
    no document, no ``interval`` in it, an ``interval`` that does not parse — and
    they collapse here rather than at the call site so there is exactly one
    fallback branch to get right. Only the *third* is a divergence worth
    reporting; ``applied_interval`` on ``info.watch-status`` is where that goes
    (CannObserv/watcher#264), and it reads this same column.
    """
    if not isinstance(watch_spec, dict):
        return None
    interval = watch_spec.get("interval")
    if interval is None:
        return None
    try:
        parse_interval(interval)
    except (TypeError, ValueError):
        logger.warning(
            "unparseable announced interval — falling back to the local cadence",
            extra={"interval": repr(interval)},
        )
        return None
    return {"interval": interval}


async def _stored_generation(session, info_item_id: str, row: WatchedItem | None) -> int | None:
    """The generation this key has applied — from the row, or its tombstone.

    A revoked key has no row, which is exactly when the ordering guard matters
    most: the stale-live-after-tombstone case would otherwise find nothing to
    compare against and resurrect a retired item.
    """
    if row is not None and row.applied_generation is not None:
        return row.applied_generation
    tomb = await session.get(RevokedInfoItem, info_item_id)
    return tomb.generation if tomb is not None else None


async def reconcile_announcement(session, payload: RegistryAnnouncementState) -> str:
    """Reconcile one announcement into ``watched_items``; returns an outcome tag.

    Commits before returning — the row is the durable record. Redelivery is a
    no-op via the generation guard, which is what makes replaying the whole
    stream at boot cheap.
    """
    try:
        info_item_ulid = ULID.from_str(payload.info_item_id)
    except (ValueError, TypeError):
        # Nothing to reconcile against and nowhere to route it. Log attributably
        # and drop; the next snapshot supersedes it.
        logger.warning(
            "announcement carries a malformed info_item_id — dropping",
            extra={"info_item_id": repr(payload.info_item_id), "generation": payload.generation},
        )
        return "invalid"

    key = str(payload.info_item_id)
    row = (
        (
            await session.execute(
                select(WatchedItem).where(WatchedItem.archiver_info_item_id == info_item_ulid)
            )
        )
        .scalars()
        .one_or_none()
    )

    stored = await _stored_generation(session, key, row)
    if stored is not None and payload.generation <= stored:
        logger.info(
            "stale announcement ignored",
            extra={"info_item_id": key, "generation": payload.generation, "stored": stored},
        )
        return "stale"

    # Branch on `revoked` FIRST — a tombstone carries no descriptive fields.
    if payload.revoked:
        if row is not None:
            await session.delete(row)
        tomb = await session.get(RevokedInfoItem, key)
        if tomb is None:
            session.add(
                RevokedInfoItem(
                    info_item_id=key,
                    generation=payload.generation,
                    revoked_at=payload.occurred_at,
                )
            )
        else:
            tomb.generation = payload.generation
            tomb.revoked_at = payload.occurred_at
        await session.commit()
        logger.info(
            "watched_item revoked",
            extra={"info_item_id": key, "generation": payload.generation},
        )
        return "revoked"

    created = row is None
    if created:
        row = WatchedItem(
            archiver_info_item_id=info_item_ulid,
            name=derive_watched_item_name(payload.url),
            effective_url=payload.url,
            archiver_info_source_id=payload.info_source_id,
        )
        session.add(row)
        # A key coming back from revoked is a live announcement, not a special
        # case: drop the tombstone so the guard tracks the row again.
        tomb = await session.get(RevokedInfoItem, key)
        if tomb is not None:
            await session.delete(tomb)

    row.archiver_info_source_id = payload.info_source_id
    row.effective_url = payload.url
    row.source_specs = list(payload.source_specs or [])
    row.announced_schedule_config = _announced_schedule_config(payload.watch_spec)

    # `active is None` is an abstention, not a default — leave the column alone.
    # There is nothing to abstain from on a create, where the model default
    # (active) stands. Applied unconditionally otherwise: a local pause is NOT
    # sticky (archiver#150), and `archived_at` is untouched either way, so an
    # `active: true` against an archived row no-ops on scheduling rather than
    # resurrecting it — the scheduler gates on `archived_at IS NULL` too.
    if payload.active is not None:
        row.is_active = payload.active

    # Re-derive the domain only when the host actually moves. Doing it on every
    # announcement would clear a `domain_suspended` an operator set — host-level
    # mechanism the registry has no opinion on.
    domain_name = domain_name_for_url(payload.url)
    if domain_name != row.domain_name:
        domain_state = await ensure_domain_and_resolve_suspension(session, domain_name)
        row.domain_name = domain_name
        row.domain_suspended = domain_state.suspended
        row.domain_default_schedule_config = domain_state.default_schedule_config

    row.applied_generation = payload.generation
    await session.commit()

    outcome = "created" if created else "updated"
    logger.info(
        "watched_item reconciled",
        extra={
            "info_item_id": key,
            "generation": payload.generation,
            "outcome": outcome,
            "watched_item_id": str(row.id),
        },
    )
    return outcome


async def _apply_with_retry(session_factory, payload: RegistryAnnouncementState) -> str | None:
    """Reconcile one payload, retrying a transient failure in memory.

    There is no PEL here and no DLQ, so a failure is retry-or-drop. Retrying
    holds ordering (the next message is not read until this one settles);
    dropping is survivable only because the periodic snapshot republishes, which
    is why it is the bounded last resort rather than the first response.
    """
    for attempt in range(1, MAX_APPLY_ATTEMPTS + 1):
        try:
            async with session_factory() as session:
                return await reconcile_announcement(session, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            if attempt == MAX_APPLY_ATTEMPTS:
                logger.error(
                    "announcement dropped after repeated failures — the next "
                    "snapshot must repair this key",
                    extra={
                        "info_item_id": getattr(payload, "info_item_id", "?"),
                        "generation": getattr(payload, "generation", "?"),
                    },
                    exc_info=True,
                )
                return None
            logger.warning(
                "reconcile failed — retrying",
                extra={"attempt": attempt, "info_item_id": getattr(payload, "info_item_id", "?")},
                exc_info=True,
            )
            await asyncio.sleep(ERROR_BACKOFF_SECONDS * attempt)
    return None


async def run_registry_consumer(
    client: Redis,
    session_factory,
    *,
    stop: asyncio.Event,
    block_ms: int = BLOCK_MS,
    error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
) -> None:
    """Replay the registry from ``0-0``, then tail it until ``stop`` is set.

    Groupless: ``info.registry`` is a config/state stream, so every consumer
    needs every message and a consumer group here would accumulate a PEL nothing
    drains. ``AsyncBusTailReader`` starts at ``0-0`` and offers no way to ask for
    ``$`` — the mistake it exists to prevent, since a worker that boots at ``$``
    reads nothing and looks exactly like a worker whose registry is empty.

    Replay is not a special mode: the generation guard makes re-reading applied
    announcements a no-op, so boot and steady state run the same code. A stream
    that has been trimmed replays fewer messages, not wrong ones — retention is a
    consumer contract carried by the producer's ``maxlen``.
    """
    reader = AsyncBusTailReader(client, topic=streams.INFO_REGISTRY)
    replayed = False

    while not stop.is_set():
        try:
            messages: list[BusMessage] = await reader.read(
                count=READ_COUNT, block_ms=None if not replayed else block_ms
            )
            if not messages:
                if not replayed:
                    replayed = True
                    logger.info("info.registry replay complete — tailing")
                    continue
                # A client that ignores `block` would busy-spin without this.
                await asyncio.sleep(IDLE_SLEEP_SECONDS)
                continue

            for message in messages:
                payload = message.payload
                if not isinstance(payload, RegistryAnnouncementState):
                    logger.info(
                        "unexpected payload type on info.registry — ignoring",
                        extra={"event_type": getattr(payload, "event_type", "?")},
                    )
                    continue
                outcome = await _apply_with_retry(session_factory, payload)
                logger.info(
                    "info.registry announcement processed",
                    extra={"message_id": message.message_id, "outcome": outcome},
                )
        except BusMessageAnomaly as exc:
            # No group, so no ack to skip past a poison frame with: the cursor
            # must be advanced explicitly or the next read raises on the same
            # frame forever. Safe at `count=1` — there is no well-formed prefix
            # to strand.
            message_id = getattr(exc, "message_id", None)
            logger.warning(
                "undecodable frame on info.registry — seeking past it",
                extra={"message_id": message_id, "error": str(exc)},
            )
            if message_id and message_id != "?":
                reader.seek(message_id)
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("info.registry consumer error — backing off", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=error_backoff_seconds)
            except TimeoutError:
                pass
            continue


def start_registry_consumer(client: Redis, session_factory, *, stop: asyncio.Event) -> asyncio.Task:
    """Spawn the consumer loop as a lifespan task (caller owns client + stop).

    The done-callback is the dead-man's switch: the lifespan never awaits this
    task until shutdown, so an escaped exception would otherwise leave the
    registry silently frozen while the process keeps serving — the exact
    silent-divergence failure this channel replaces.
    """
    task = asyncio.create_task(run_registry_consumer(client, session_factory, stop=stop))

    def _observe(t: asyncio.Task) -> None:
        if t.cancelled() or stop.is_set():
            return  # orderly shutdown
        exc = t.exception()
        if exc is not None:
            logger.critical(
                "info.registry consumer task DIED — the registry will diverge until restart",
                exc_info=exc,
            )
        else:
            logger.critical("info.registry consumer task exited unexpectedly")

    task.add_done_callback(_observe)
    return task
