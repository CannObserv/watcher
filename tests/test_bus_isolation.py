"""The test session must never be able to reach a real bus.

Pins the conftest import-time guard. This is not hypothetical: a run under an
exported ``/etc/watcher/.env`` published a fabricated ``source_revision_observed``
frame onto the **production** ``content.revisions`` stream. It was inert —
Archiver's consumer dropped it as an unknown ``info_source`` — but a test reached
production Redis, which is the #233 database hazard with a different variable.

The guard is the same shape as the DATABASE_URL one: clear the variable at
import so "no bus" is the default, and let a test that wants one inject a
``fakeredis`` client explicitly.
"""

import os

from src.core.bus import bus_client_from_env, get_shared_bus_client


class TestBusIsolation:
    def test_bus_url_is_cleared_for_the_session(self) -> None:
        assert os.environ.get("WATCHER_BUS_REDIS_URL") is None
        assert os.environ.get("WATCHER_DEV_BUS_REDIS_URL") is None

    def test_env_client_construction_yields_nothing(self) -> None:
        """The producers' own resolver must come back empty, not connected."""
        assert bus_client_from_env() is None

    def test_shared_client_yields_nothing(self) -> None:
        """The path a task takes when no client is injected (#253's drain).

        If this ever returns a client, a test that forgets to pass a fakeredis
        instance publishes onto whatever stream the environment names.
        """
        assert get_shared_bus_client() is None
