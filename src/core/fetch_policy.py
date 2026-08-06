"""The content.fetch-policy producer — Watcher's half of the politeness split (#245).

Replicator enforces per-host request spacing (mechanism); Watcher owns the
numbers (policy) and publishes them here as ``FetchPolicyState`` frames on
``content.fetch-policy`` (contract: cannobserv#285; consumer: replicator#19).
Last-write-wins per ``host``, so correctness rests on two producer-side rules:

* **Full-set republish.** The consumer replays the stream from ``0-0`` at boot,
  so its view must be reconstructible from what remains on the broker — the
  periodic task republishes every policy, not just changed ones.
* **Tombstones are republished too.** A revoked host (deleted Domain) keeps its
  ``revoked=True`` frame in every full set; dropping it would let broker
  trimming age the tombstone out from under a booting consumer. The
  ``fetch_policy_tombstones`` table carries that obligation past the Domain
  row's deletion.

The published interval is ``Domain.min_interval`` — the operator floor — never
``current_interval``: that column is 429-backoff *state*, and its feed dies at
the Phase-4 cutover (no non-terminal ``fetch_failed``; replicator#9 §3).
Adaptive backoff is Replicator's follow-on (replicator#25).

Publishing goes through co-core's ``to_wire`` over the strict ``FetchPolicyEmit``
(``extra="forbid"``) — never hand-rolled fields (issuer-contract rule zero).
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import to_wire
from co_core.pure.models.changes import FetchPolicyEmit
from co_core_aio.bus import AsyncBusPublisher
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.core.models.domain import Domain
from src.core.models.fetch_policy_tombstone import FetchPolicyTombstone

logger = get_logger(__name__)

# Bus client construction lives in src.core.bus (#241 CR-4/CR-10) — import
# BUS_REDIS_URL_ENV / bus_client_from_env from there.


def build_policy_events(
    domains: Sequence[Domain],
    tombstones: Sequence[FetchPolicyTombstone],
    *,
    now: datetime,
) -> list[FetchPolicyEmit]:
    """One ``FetchPolicyEmit`` per domain (live) and per tombstone (revoked).

    A host the model rejects (non-ASCII, embedded port, …) is skipped with a
    warning rather than failing the batch — one unpublishable row must not stop
    the rest of the corpus's policies from travelling. ``Domain.name`` comes
    from ``urlparse().hostname`` so rejections should be rare; the warning is
    the signal that one slipped through.
    """
    events: list[FetchPolicyEmit] = []
    for domain in domains:
        try:
            events.append(
                FetchPolicyEmit(
                    occurred_at=now,
                    host=domain.name,
                    min_interval_seconds=domain.min_interval,
                )
            )
        except ValidationError:
            logger.warning(
                "skipping unpublishable fetch-policy host",
                extra={"host": domain.name, "kind": "domain"},
            )
    for tombstone in tombstones:
        try:
            events.append(FetchPolicyEmit(occurred_at=now, host=tombstone.host, revoked=True))
        except ValidationError:
            logger.warning(
                "skipping unpublishable fetch-policy host",
                extra={"host": tombstone.host, "kind": "tombstone"},
            )
    return events


async def publish_policy_events(client: Redis, events: Sequence[FetchPolicyEmit]) -> int:
    """XADD each event to ``content.fetch-policy``; returns the count published."""
    publisher = AsyncBusPublisher(client)
    for event in events:
        await publisher.execute(BusPublish(streams.CONTENT_FETCH_POLICY, to_wire(event)))
    return len(events)


async def publish_full_policy_set(session: AsyncSession, client: Redis) -> int:
    """Publish the whole policy set: every Domain, plus every tombstone.

    All domains publish — paused/archived domains still carry the operator's
    politeness intent for their host, and a frame for a host nobody fetches is
    harmless where a missing tombstone is not.
    """
    domains = (await session.execute(select(Domain))).scalars().all()
    tombstones = (await session.execute(select(FetchPolicyTombstone))).scalars().all()
    events = build_policy_events(domains, tombstones, now=datetime.now(UTC))
    return await publish_policy_events(client, events)


async def record_tombstone(
    session: AsyncSession, host: str, *, now: datetime | None = None
) -> None:
    """Upsert the tombstone row for ``host`` (idempotent; called on domain delete).

    Does not commit — the caller owns the transaction, so the tombstone lands
    atomically with the Domain delete it records.
    """
    await session.merge(
        FetchPolicyTombstone(host=host, revoked_at=now if now is not None else datetime.now(UTC))
    )


async def clear_tombstone(session: AsyncSession, host: str) -> None:
    """Remove ``host``'s tombstone, if any (called when a Domain is re-created)."""
    await session.execute(delete(FetchPolicyTombstone).where(FetchPolicyTombstone.host == host))
