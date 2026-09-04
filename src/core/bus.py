"""Bus client construction and the process-shared client (#241 CR finding 4).

One long-lived ``redis.asyncio.Redis`` per process, matching co-core's driver
model ("bus clients are long-lived and shared; each consumer service owns one
for its whole run"). ``Redis`` is a connection *pool*, so the blocking consumer
read and concurrent publishes coexist on one client. Before this, every issued
command opened and closed its own TCP connection — churn that scales with the
corpus in bus mode.

Ownership: the lifespan closes the shared client at shutdown
(``aclose_shared_bus_client``). Callers must therefore never ``aclose`` what
``get_shared_bus_client`` returns; test seams and scripts that build their own
client via ``bus_client_from_env`` keep owning theirs.

The env var is service-prefixed per the AGENTS.md naming rule: a bare
``REDIS_URL`` would be silently inherited from ``/etc/watcher/.env`` by
anything that sources it (the #233 hazard in env-var form). Unset means "no
bus": producers skip loudly, the consumer never starts.

A URL is *configuration*, not *permission* (#262). Anything that sources
``/etc/watcher/.env`` inherits the production broker address — an agent shell,
a one-off script, a ``python -c``, a REPL — and before the gate below, merely
importing a producer in one of those was enough to reach the live streams. That
is not the "merely noisy stray producer" archiver#139 reasoned about: a stray
``content.fetch`` makes Replicator issue **real origin requests** against
government portals under Watcher's pinned User-Agent; ``content.fetch-policy``
is last-write-wins per host, so a dev database's numbers become cluster-wide
politeness instruction until the next full-set republish; and
``content.revisions`` writes into **Archiver's** registry, which a dev database
seeded from production would fill with real-looking rows. So publishing (and
consuming) additionally requires ``WATCHER_BUS_ENABLED=1``, which only
``deploy/watcher.service`` and ``scripts/dev_server.sh``'s scratch-bus branch
set — never an env file, for the same reason as
``WATCHER_ALLOW_PRODUCTION_DB`` (#233).
"""

import os
import time
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from src.core import read_windows
from src.core.logging import get_logger

logger = get_logger(__name__)

BUS_REDIS_URL_ENV = "WATCHER_BUS_REDIS_URL"

#: Unit-only opt-in gating every bus client this process can build (#262).
BUS_ENABLED_ENV = "WATCHER_BUS_ENABLED"

# --- Connection policy (#287, CannObserv/broker#1 R7) --------------------------
#
# Until now this was a bare ``Redis.from_url`` with library defaults, which
# loopback made harmless: a local broker either answers in microseconds or
# refuses immediately. Neither is true across the tailnet hop to the relocated
# broker, where the measured path is a ~40 ms DERP relay that never establishes
# a direct connection (24 pings, two runs, all relayed). A *stalled* broker — as
# distinct from a refusing one — would hold a read open with no bound, and
# because ``get_shared_bus_client`` pins one client for the process, the wedge
# sits inside a service whose ``/health`` knows nothing about the bus.

# Headroom over the longest blocking read. It absorbs the round trip plus the
# broker's own scheduling slack, and it is the difference between "the read
# window elapsed" and "the socket is stalled". Generous against a ~40 ms path on
# purpose: too tight spins the consumers, too loose notices a stall late.
BLOCKING_READ_MARGIN_SECONDS = 5.0

#: **``socket_timeout`` has a floor, not a ceiling.** redis-py does not extend it
#: for a blocking command — measured on 7.4.1, ``socket_timeout=1`` with
#: ``XREAD ... BLOCK 3000`` raised after 1.01 s, while ``socket_timeout=10``
#: returned normally after 3.08 s. Both Watcher loops block for 5 s, so any value
#: at or below that does not bound a stall, it manufactures one on every idle
#: read. Derived from the leaf rather than transcribed beside a comment naming
#: it: the two windows live in different modules and can drift apart.
SOCKET_TIMEOUT_SECONDS = read_windows.LONGEST_BLOCK_MS / 1000 + BLOCKING_READ_MARGIN_SECONDS

# Connecting carries no BLOCK, so it needs no headroom and should fail fast: a
# broker that is down, mis-addressed, or black-holed by an ACL change is what
# this bounds, and a ~40 ms path clears it by two orders of magnitude.
SOCKET_CONNECT_TIMEOUT_SECONDS = 5.0

# PING a connection idle longer than this before reusing it, so a silently
# dropped TCP session surfaces as a retryable error on the next command instead
# of a first-write failure. Relevant across a relay in a way it never was on
# loopback, where nothing sat between the two ends to time a session out.
HEALTH_CHECK_INTERVAL_SECONDS = 30

#: ZERO retries, stated rather than inherited.
#:
#: **A redis-py retry re-sends the command; it does not resume the response.**
#: ``Redis.execute_command`` wraps ``_send_command_parse_response`` in
#: ``Retry.call_with_retry``, so a ``TimeoutError`` raised *after* the broker
#: already applied an ``XADD`` publishes the entry a second time.
#:
#: Watcher's exposure is on the producer side — ``content.fetch``,
#: ``content.fetch-policy``, ``info.watch-status``, ``content.revisions``.
#: Duplicates on the two last-write-wins config streams are absorbed by
#: construction, but ``content.fetch`` is a command stream with a consumer group,
#: and a duplicated command is a duplicated fetch: a second real origin request
#: against a government portal under Watcher's pinned User-Agent.
#:
#: Nothing is given up by declining it. Both consumer loops already back off on
#: any error and retry, the fetch-command outbox is the durable buffer behind the
#: publisher, and the stale-pooled-connection case a retry would have covered is
#: what ``health_check_interval`` above is for. redis-py's *default* ``Retry`` is
#: also zero — the object reads as a policy while behaving as none, and
#: ``retry_on_timeout=True`` raises it to one — so passing the zero explicitly
#: makes it a decision a future change has to argue with.
BUS_RETRIES = 0

# What an operator actually waits on an unreachable broker.
# ``socket_connect_timeout`` bounds a single *attempt*; a retry multiplies it.
# Measured against a black-holed address on redis-py 7.4.1: connect=5/retries=0
# raised at 5.01 s, connect=5/retries=1 at 10.03 s, connect=2/retries=1 at
# 4.02 s. At ``BUS_RETRIES = 0`` the two collapse, and it is worth keeping
# expressed as the product anyway: the factor is what makes a retry added later
# cost twice what its own diff appears to say.
#
# This is why the startup probe below runs detached rather than inline in the
# lifespan: even five seconds of a blocked lifespan is five seconds the
# dashboard does not serve, and the dashboard has no business being unavailable
# because the bus is.
WORST_CASE_CONNECT_SECONDS = (BUS_RETRIES + 1) * SOCKET_CONNECT_TIMEOUT_SECONDS

_shared_client: Redis | None = None


class BusNotEnabled(RuntimeError):
    """Raised when a process holds a bus URL it was never authorised to use."""


def bus_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when the caller explicitly opted this process into the bus.

    Only the exact string ``"1"`` opts in. A fuzzy truthiness check would let a
    stray value quietly re-open the hole the gate closes — the same rule
    ``src.core.db_safety`` applies to ``WATCHER_ALLOW_PRODUCTION_DB``.
    """
    if environ is None:
        environ = os.environ
    return environ.get(BUS_ENABLED_ENV) == "1"


def bus_disabled_reason(environ: Mapping[str, str] | None = None) -> str | None:
    """Why no bus client can be built, or None when one can.

    Producers log and return this verbatim, so the operator reads the variable
    that is actually missing rather than the one the check happened to test
    first.
    """
    if environ is None:
        environ = os.environ
    if not environ.get(BUS_REDIS_URL_ENV):
        return f"{BUS_REDIS_URL_ENV} not set"
    if not bus_enabled(environ):
        return f"{BUS_ENABLED_ENV} is not 1"
    return None


def assert_environment_bus_allowed(environ: Mapping[str, str]) -> None:
    """Refuse a bus URL that was never opted into (#262, the loud half).

    :func:`bus_client_from_env` already fails *closed* on this combination, but
    closed-and-silent trades one production hazard for another: drop
    ``Environment=WATCHER_BUS_ENABLED=1`` from the unit and Watcher stops
    publishing with nothing but a per-producer ERROR to say so. A URL present
    without the opt-in is always a mistake in either direction — a service that
    lost its flag, or a process that should never have had the URL — so the
    entry point refuses to start.

    Not raised when the URL is absent: that names no broker, so nothing can be
    published by accident, and making it fatal would stop every dev server and
    script that never wanted a bus. The producers' existing loud skip covers it.

    Takes ``environ`` explicitly, like ``assert_environment_db_allowed``: the
    caller decides what is being gated, and this stays callable from a test
    even though ``tests/conftest.py`` clears the real variable at import.
    """
    if not environ.get(BUS_REDIS_URL_ENV):
        return
    if bus_enabled(environ):
        return
    raise BusNotEnabled(
        f"refusing to start: {BUS_REDIS_URL_ENV} is set but {BUS_ENABLED_ENV} is not 1, "
        "so this process holds a broker address it is not authorised to use.\n"
        f"  Only deploy/watcher.service may publish to the production bus; it sets "
        f"{BUS_ENABLED_ENV}=1. If this IS the service, that line is missing from the "
        "installed unit.\n"
        "  For a dev server use: bash scripts/dev_server.sh (set "
        "WATCHER_DEV_BUS_REDIS_URL for a scratch bus).\n"
        f"  For anything else, unset {BUS_REDIS_URL_ENV}. (See #262 and the #253 "
        "incident it was filed from.)"
    )


def bus_client_from_env() -> Redis | None:
    """A NEW Redis client for the bus, or None when the env does not allow one.

    Requires both a URL and ``WATCHER_BUS_ENABLED=1``. This is the single funnel
    for client *construction* — ``get_shared_bus_client`` and every direct caller
    pass through it — so the gate covers publish *and* consume without a second
    mechanism: producers already treat a None client as a loud skip, and the
    consumer never starts.

    It is **not** the only thing that asks whether the bus is usable. A caller
    that must *explain* the absence (the producers, which log which variable is
    missing before skipping) asks :func:`bus_disabled_reason` instead. #262's
    premise that a single funnel covered both readings was wrong — two producers
    tested ``WATCHER_BUS_REDIS_URL`` directly and then asserted on the client,
    which this gate would have turned into an AssertionError. Do not reintroduce
    a bare env check; the two functions share one predicate below so they cannot
    disagree.

    No localhost default, deliberately: a default credential is how a dev
    process ends up publishing onto the production stream. The caller owns the
    returned client's lifecycle — for the process-shared one, use
    :func:`get_shared_bus_client`.

    Being the single funnel is also what makes the connection policy above
    universal (#287): every client this process can build — shared, injected, or
    script-owned — carries the same timeouts, so there is no second construction
    site to keep in step.
    """
    if bus_disabled_reason() is not None:
        return None
    return Redis.from_url(
        os.environ[BUS_REDIS_URL_ENV],
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=SOCKET_CONNECT_TIMEOUT_SECONDS,
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
        # ``retry_on_error`` is deliberately absent: redis-py's Retry already
        # carries exactly (ConnectionError, TimeoutError), so passing the same
        # pair would imply this widens something it does not.
        retry=Retry(ExponentialBackoff(), retries=BUS_RETRIES),
    )


def get_shared_bus_client() -> Redis | None:
    """The process-shared bus client, built lazily; None when the env forbids one.

    Never close the returned client — the lifespan owns it. The env var is
    re-read while unbuilt, so a test that sets the var after import still gets
    a client; once built, the client is pinned until ``aclose_shared_bus_client``.
    """
    global _shared_client
    if _shared_client is None:
        _shared_client = bus_client_from_env()
    return _shared_client


async def aclose_shared_bus_client() -> None:
    """Close and forget the shared client (lifespan shutdown; test teardown)."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


_REDACTED = "***"


class SupportsPing(Protocol):
    """The only thing the probe needs from a client.

    Narrower than ``Redis`` on purpose: it says what is actually required, and it
    lets a test hand in a two-line stub without a type ignore.
    """

    async def ping(self) -> Any: ...


def redact_url(redis_url: str) -> str:
    """Return ``redis_url`` with any password replaced.

    The broker gains a ``requirepass`` when it moves to its own node
    (CannObserv/broker#1 D3), and every line below goes to journald. Fails
    *closed*: a URL that will not parse is replaced wholesale rather than passed
    through, because the moment redaction is hardest is the moment a malformed
    URL is the thing being reported.

    The host is rebuilt rather than sliced out, which costs two edge cases worth
    naming. ``urlsplit().hostname`` strips IPv6 brackets, so they have to be
    restored or the result stops being a URL; and it lower-cases, which is
    harmless for DNS but means the line is not byte-identical to what the
    operator configured. Both matter because ``redis_url`` is the one field
    identifying *which* broker is down, and it is emitted at ERROR mid-incident.
    """
    try:
        parts = urlsplit(redis_url)
        if parts.password is None:
            return redis_url
        host = parts.hostname or ""
        if ":" in host:  # IPv6 literal — urlsplit strips the brackets
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        userinfo = f"{parts.username or ''}:{_REDACTED}"
        return urlunsplit(
            (parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment)
        )
    except ValueError:
        return "<unparseable redis url>"


async def probe_bus_reachable(client: SupportsPing, redis_url: str) -> bool:
    """PING the broker once at startup and say so, loudly, either way (#287).

    ``from_url`` is **lazy**: it returns against a broker with nothing listening
    and raises nothing, so the lifespan's wiring never sees an unreachable
    broker. The process starts, both consumers are scheduled, and the first real
    failure lands inside a loop that correctly treats it as transient and backs
    off — correct behaviour for a partition, and indistinguishable from an idle
    cluster for a misconfiguration. Watcher makes that reading worse than
    Archiver's: with no fact inbox nothing can complete, so "quiet" is exactly
    what a healthy idle Watcher looks like too.

    Never raises. It is spawned detached, so an exception here would surface as
    a bare "Task exception was never retrieved" and set the diagnosis back.
    ``BaseException`` is deliberately not caught: a ``CancelledError`` at
    shutdown must propagate or the lifespan's gather would never join it.
    """
    started = time.monotonic()
    try:
        await client.ping()
    except Exception as e:
        logger.error(
            "Bus broker is configured but unreachable — the bus is NOT idle, it is down",
            extra={
                "redis_url": redact_url(redis_url),
                "error": f"{type(e).__name__}: {e}",
                "waited_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return False
    logger.info(
        "Bus broker reachable",
        extra={
            "redis_url": redact_url(redis_url),
            "rtt_ms": round((time.monotonic() - started) * 1000),
        },
    )
    return True


def resolve_stream_maxlen(env_name: str, default: int) -> int:
    """Parse a stream-retention cap from ``env_name`` defensively; never unbounded.

    Config/state streams (``content.fetch-policy``, ``info.watch-status``)
    republish their full set periodically, so an untrimmed stream grows without
    bound and a consumer's replay-from-``0-0`` boot cost tracks *history*
    rather than corpus size (watcher#264 CR-1/CR-3; the pattern mirrors
    Archiver's ``resolve_registry_maxlen``). An invalid or non-positive value
    falls back to ``default`` with a warning — misconfiguration must degrade
    to bounded, not to unbounded.
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid %s — falling back to default",
            env_name,
            extra={"value": raw, "default": default},
        )
        return default
    if value <= 0:
        logger.warning(
            "non-positive %s — falling back to default",
            env_name,
            extra={"value": raw, "default": default},
        )
        return default
    return value
