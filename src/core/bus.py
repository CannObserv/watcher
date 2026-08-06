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
"""

import os

from redis.asyncio import Redis

from src.core.logging import get_logger

logger = get_logger(__name__)

BUS_REDIS_URL_ENV = "WATCHER_BUS_REDIS_URL"

_shared_client: Redis | None = None


def bus_client_from_env() -> Redis | None:
    """A NEW Redis client for the bus, or None when the env var is unset.

    No localhost default, deliberately: a default credential is how a dev
    process ends up publishing onto the production stream. The caller owns the
    returned client's lifecycle — for the process-shared one, use
    :func:`get_shared_bus_client`.
    """
    url = os.environ.get(BUS_REDIS_URL_ENV)
    if not url:
        return None
    return Redis.from_url(url)


def get_shared_bus_client() -> Redis | None:
    """The process-shared bus client, built lazily; None when the env is unset.

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
