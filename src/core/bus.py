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
from collections.abc import Mapping

from redis.asyncio import Redis

from src.core.logging import get_logger

logger = get_logger(__name__)

BUS_REDIS_URL_ENV = "WATCHER_BUS_REDIS_URL"

#: Unit-only opt-in gating every bus client this process can build (#262).
BUS_ENABLED_ENV = "WATCHER_BUS_ENABLED"

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
    """
    if bus_disabled_reason() is not None:
        return None
    return Redis.from_url(os.environ[BUS_REDIS_URL_ENV])


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
