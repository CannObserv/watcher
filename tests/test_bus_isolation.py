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

import pytest

from src.core.bus import (
    BUS_ENABLED_ENV,
    BUS_REDIS_URL_ENV,
    BusNotEnabled,
    assert_environment_bus_allowed,
    bus_client_from_env,
    bus_disabled_reason,
    bus_enabled,
    get_shared_bus_client,
)


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


class TestBusEnabledGate:
    """#262: a URL is not permission — ``WATCHER_BUS_ENABLED=1`` is.

    Any process that sources ``/etc/watcher/.env`` inherits the production
    ``WATCHER_BUS_REDIS_URL``: an agent shell, a one-off script, a REPL. Before
    this gate, importing a producer in one of those was enough to publish onto
    the live streams — a stray ``content.fetch`` makes Replicator hit real
    government portals, a stray ``content.fetch-policy`` overwrites cluster
    politeness last-write-wins, and a stray ``content.revisions`` writes into
    Archiver's registry. The flag lives only in ``deploy/watcher.service``, so
    nothing that merely sources an env file can carry it.
    """

    def test_a_url_alone_builds_no_client(self, monkeypatch) -> None:
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
        monkeypatch.delenv(BUS_ENABLED_ENV, raising=False)
        assert bus_client_from_env() is None

    def test_the_flag_alone_builds_no_client(self, monkeypatch) -> None:
        """The flag is permission, not configuration — it names no broker."""
        monkeypatch.delenv(BUS_REDIS_URL_ENV, raising=False)
        monkeypatch.setenv(BUS_ENABLED_ENV, "1")
        assert bus_client_from_env() is None

    @pytest.mark.parametrize("value", ["", "0", "true", "yes", "TRUE", " 1"])
    def test_only_the_exact_string_one_opts_in(self, monkeypatch, value: str) -> None:
        """Mirrors ``WATCHER_ALLOW_PRODUCTION_DB`` (#233): a fuzzy truthiness
        check would let a stray value quietly re-open the hole."""
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
        monkeypatch.setenv(BUS_ENABLED_ENV, value)
        assert bus_enabled() is False
        assert bus_client_from_env() is None

    async def test_url_plus_flag_builds_a_client(self, monkeypatch) -> None:
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
        monkeypatch.setenv(BUS_ENABLED_ENV, "1")
        client = bus_client_from_env()
        assert client is not None
        # from_url is lazy — nothing has connected — but the caller owns it.
        await client.aclose()


class TestBusDisabledReason:
    """The producers' skip message must name the variable actually missing."""

    def test_no_url_reports_the_url(self, monkeypatch) -> None:
        monkeypatch.delenv(BUS_REDIS_URL_ENV, raising=False)
        monkeypatch.delenv(BUS_ENABLED_ENV, raising=False)
        assert bus_disabled_reason() == f"{BUS_REDIS_URL_ENV} not set"

    def test_url_without_the_flag_reports_the_flag(self, monkeypatch) -> None:
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
        monkeypatch.delenv(BUS_ENABLED_ENV, raising=False)
        assert bus_disabled_reason() == f"{BUS_ENABLED_ENV} is not 1"

    def test_both_present_is_no_reason_at_all(self, monkeypatch) -> None:
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
        monkeypatch.setenv(BUS_ENABLED_ENV, "1")
        assert bus_disabled_reason() is None


class TestEnvironmentBusGate:
    """The loud half: a URL without the flag is a misconfiguration, not a mode.

    Failing closed *and* silently would trade one production hazard for another
    — forget ``Environment=WATCHER_BUS_ENABLED=1`` in the unit and Watcher stops
    publishing. So the one combination that is always a mistake in either
    direction refuses to start (#262, option 2).
    """

    def test_url_without_the_flag_refuses(self) -> None:
        with pytest.raises(BusNotEnabled) as excinfo:
            assert_environment_bus_allowed(
                {BUS_REDIS_URL_ENV: "redis://localhost:6379/0"},
            )
        message = str(excinfo.value)
        assert BUS_ENABLED_ENV in message
        assert "deploy/watcher.service" in message
        assert "scripts/dev_server.sh" in message

    def test_url_with_a_non_opt_in_value_refuses(self) -> None:
        with pytest.raises(BusNotEnabled):
            assert_environment_bus_allowed(
                {BUS_REDIS_URL_ENV: "redis://localhost:6379/0", BUS_ENABLED_ENV: "true"},
            )

    def test_url_with_the_flag_is_allowed(self) -> None:
        assert_environment_bus_allowed(
            {BUS_REDIS_URL_ENV: "redis://localhost:6379/0", BUS_ENABLED_ENV: "1"},
        )

    def test_no_url_is_allowed_flag_or_not(self) -> None:
        """No URL names no broker, so nothing can be published by accident.

        The producers' existing loud skip is the signal for that case; making
        it fatal would stop every dev server and script that never wanted a bus.
        """
        assert_environment_bus_allowed({})
        assert_environment_bus_allowed({BUS_ENABLED_ENV: "1"})

    def test_the_gate_reads_the_mapping_not_the_process(self, monkeypatch) -> None:
        """Explicit ``environ`` argument, like ``assert_environment_db_allowed``:
        the caller decides what is being gated, and a test can reach the check
        even though conftest clears the real variable at import."""
        monkeypatch.delenv(BUS_REDIS_URL_ENV, raising=False)
        with pytest.raises(BusNotEnabled):
            assert_environment_bus_allowed({BUS_REDIS_URL_ENV: "redis://localhost:6379/0"})
        assert os.environ.get(BUS_REDIS_URL_ENV) is None
