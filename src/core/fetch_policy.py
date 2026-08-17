"""The content.fetch-policy producer — Watcher's half of the politeness split (#245).

Replicator enforces per-host request spacing (mechanism); Watcher owns the
numbers (policy) and publishes them here as ``FetchPolicyState`` frames on
``content.fetch-policy`` (contract: cannobserv#285; consumer: replicator#19).
Last-write-wins per ``host``, so correctness rests on two producer-side rules:

* **Full-set republish.** The consumer replays the stream from ``0-0`` at boot,
  so its view must be reconstructible from what remains on the broker — the
  periodic task republishes every policy, not just changed ones.
* **Tombstones are republished too.** A revoked host keeps its ``revoked=True``
  frame in every full set; dropping it would let broker trimming age the
  tombstone out from under a booting consumer. The ``fetch_policy_tombstones``
  table carries that obligation past a Domain row's deletion; a *suspended*
  Domain needs no table at all, because its row survives (#250).

**Suspended for any reason ⇒ no live policy published (#250).** A domain that is
archived or deactivated publishes ``revoked=True``, not its ``min_interval``.
``revoked`` is the contract's tombstone — "no explicit policy for this host",
*not* "no limit": the consumer falls back to its own conservative default, which
rule 1 requires be at least as strict as anything a producer would publish. So
revocation cannot open a politeness gap, and republishing a live interval for a
host Watcher has stopped watching is the one way that host ends up *looser* than
the fallback. Restore and reactivate need no bookkeeping: the next full set reads
the cleared columns and emits live again.

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

from src.core.bus import resolve_stream_maxlen
from src.core.logging import get_logger
from src.core.models.domain import Domain
from src.core.models.fetch_policy_tombstone import FetchPolicyTombstone

logger = get_logger(__name__)

FETCH_POLICY_STREAM_MAXLEN_ENV = "WATCHER_FETCH_POLICY_STREAM_MAXLEN"
# Producer-enforced retention (watcher#264 CR-3): the full set republishes
# every 5 minutes forever, and prod had accumulated ~7k untrimmed entries.
DEFAULT_FETCH_POLICY_STREAM_MAXLEN = 50_000

# Bus client construction lives in src.core.bus (#241 CR-4/CR-10) — import
# BUS_REDIS_URL_ENV / bus_client_from_env from there.


def is_suspended(domain: Domain) -> bool:
    """Whether ``domain`` is archived or deactivated — either revokes its policy.

    Both states already stop Watcher issuing fetch commands for the domain's
    items (``ensure_domain_and_resolve_suspension`` sets ``domain_suspended``),
    so a live policy for the host asserts configuration Watcher no longer acts
    on. One predicate so the two states cannot drift apart (#250).
    """
    return domain.archived_at is not None or not domain.is_active


def build_policy_events(
    domains: Sequence[Domain],
    tombstones: Sequence[FetchPolicyTombstone],
    *,
    now: datetime,
) -> list[FetchPolicyEmit]:
    """One ``FetchPolicyEmit`` per domain and per tombstone.

    A domain emits its live ``min_interval`` unless it is suspended — archived
    or deactivated — in which case it emits ``revoked=True`` with no interval
    (#250; see the module docstring for why that is the safe direction). Every
    domain still emits, suspended or not: contract rule 2 requires revoked hosts
    keep appearing in the full set.

    A host the model rejects (non-ASCII, embedded port, …) is skipped with a
    warning rather than failing the batch — one unpublishable row must not stop
    the rest of the corpus's policies from travelling. ``Domain.name`` comes
    from ``urlparse().hostname`` so rejections should be rare; the warning is
    the signal that one slipped through.
    """
    events: list[FetchPolicyEmit] = []
    for domain in domains:
        revoked = is_suspended(domain)
        try:
            events.append(
                FetchPolicyEmit(
                    occurred_at=now,
                    host=domain.name,
                    # None, never a fake number: a consumer that ignores
                    # `revoked` gets an arithmetic failure, not a stale value.
                    min_interval_seconds=None if revoked else domain.min_interval,
                    revoked=revoked,
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
    """XADD each event to ``content.fetch-policy``; returns the count published.

    Every publish carries ``maxlen`` (approximate trim) — same producer-enforced
    retention rule as ``info.watch-status`` (watcher#264 CR-1/CR-3): a
    periodically-republished full set on an untrimmed stream grows without bound.
    """
    maxlen = resolve_stream_maxlen(
        FETCH_POLICY_STREAM_MAXLEN_ENV, DEFAULT_FETCH_POLICY_STREAM_MAXLEN
    )
    publisher = AsyncBusPublisher(client)
    for event in events:
        await publisher.execute(
            BusPublish(streams.CONTENT_FETCH_POLICY, to_wire(event), maxlen=maxlen)
        )
    return len(events)


async def publish_full_policy_set(session: AsyncSession, client: Redis) -> int:
    """Publish the whole policy set: every Domain, plus every tombstone.

    The Domain query is deliberately unfiltered. A suspended domain is not
    dropped from the set — it is emitted as ``revoked=True`` (#250), which is
    what keeps contract rule 2 satisfied without any archive/restore
    bookkeeping: the row survives, so the tombstone keeps being republished for
    as long as the domain stays suspended, and stops the moment it is restored.
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
