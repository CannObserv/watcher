"""The test session must never be able to reach the production notifier.

Third sibling of ``test_db_isolation`` and ``test_bus_isolation``, and the last
outbound production credential to get a gate (#277).

``/etc/watcher/.env`` carries ``NOTIFIER_BASE_URL`` and ``NOTIFIER_API_KEY``,
and AGENTS.md tells every agent to ``source scripts/load-env.sh`` before
running pytest. Before this, ``get_notifier_client()`` read both straight from
``os.environ`` and raised only when they were *unset* — so a suite, a hand-run
dev server, a script or a REPL in a prod-sourced shell dispatched for real, to
the production notifier, as the production tenant, and the dispatch *succeeded
silently*.

Two mechanisms, deliberately, because they cover different things:

* The **scrub** below makes "no notifier" the default for pytest specifically.
  A test that wants one sets the variables via ``monkeypatch.setenv``, which
  restores itself on teardown.
* The **gate** (``NOTIFIER_ENABLED=1``) covers every other launch path at once,
  which a scrub cannot: a scrub has to be written once per entry point, and
  nothing scrubs a ``python -c``, an agent shell, or ``scripts/*.py``.

The gate is the load-bearing half. When #277 was filed the whole suite already
passed with the base URL pointed at a black hole — no test reached the real
client — so the residue rows that prompted it came from a hand-run path the
scrub would never have covered.
"""

import os

import pytest

from src.core.notifier_client import (
    NOTIFIER_API_KEY_ENV,
    NOTIFIER_BASE_URL_ENV,
    NOTIFIER_ENABLED_ENV,
    NotifierNotEnabled,
    assert_environment_notifier_allowed,
    get_notifier_client,
    notifier_enabled,
)


class TestNotifierIsolation:
    def test_notifier_env_is_cleared_for_the_session(self) -> None:
        assert os.environ.get(NOTIFIER_BASE_URL_ENV) is None
        assert os.environ.get(NOTIFIER_API_KEY_ENV) is None

    def test_client_construction_raises_rather_than_connecting(self) -> None:
        """The path any unpatched test takes: a refusal, not a dispatch."""
        with pytest.raises(RuntimeError, match=f"{NOTIFIER_BASE_URL_ENV} .* is required"):
            get_notifier_client()

    def test_use_remote_notify_is_not_scrubbed_because_nothing_reads_it(self) -> None:
        """#277 proposed scrubbing three variables; only two are real.

        ``USE_REMOTE_NOTIFY`` has had no reader in ``src/`` since the local
        Apprise path was removed — see
        ``tests/core/notifications/test_notify_remote_only.py``, which asserts
        it is *ignored*. Scrubbing it would advertise a switch that does not
        exist, and would quietly start passing if one were ever reintroduced.
        This pins the omission as deliberate.
        """
        import src.core.notifier_client.client as client_module

        assert "USE_REMOTE_NOTIFY" not in client_module.__dict__


class TestNotifierEnabledGate:
    """#277: a URL is not permission — ``NOTIFIER_ENABLED=1`` is.

    The same rule ``src.core.bus`` applies to the broker address (#262) and
    ``src.core.db_safety`` applies to the production database (#233). Only the
    exact string ``"1"`` opts in; a fuzzy truthiness check would let a stray
    value quietly re-open the hole.
    """

    def test_exact_one_opts_in(self) -> None:
        assert notifier_enabled({NOTIFIER_ENABLED_ENV: "1"}) is True

    @pytest.mark.parametrize("value", ["true", "yes", "0", "", "1 "])
    def test_anything_else_does_not(self, value: str) -> None:
        assert notifier_enabled({NOTIFIER_ENABLED_ENV: value}) is False

    def test_absent_does_not(self) -> None:
        assert notifier_enabled({}) is False

    def test_client_refuses_a_url_held_without_the_flag(self, monkeypatch) -> None:
        monkeypatch.setenv(NOTIFIER_BASE_URL_ENV, "http://localhost:9000")
        monkeypatch.setenv(NOTIFIER_API_KEY_ENV, "nk_test")
        monkeypatch.delenv(NOTIFIER_ENABLED_ENV, raising=False)

        with pytest.raises(NotifierNotEnabled) as excinfo:
            get_notifier_client()
        assert NOTIFIER_ENABLED_ENV in str(excinfo.value)

    def test_missing_url_is_still_the_url_error(self, monkeypatch) -> None:
        """The operator reads the variable that is actually missing.

        Same ordering rule as ``bus_disabled_reason``: reporting the flag first
        would send someone hunting for an opt-in when the real problem is that
        no notifier was configured at all.
        """
        monkeypatch.delenv(NOTIFIER_BASE_URL_ENV, raising=False)
        monkeypatch.setenv(NOTIFIER_ENABLED_ENV, "1")

        with pytest.raises(RuntimeError, match=NOTIFIER_BASE_URL_ENV):
            get_notifier_client()

    def test_missing_key_is_still_the_key_error(self, monkeypatch) -> None:
        monkeypatch.setenv(NOTIFIER_BASE_URL_ENV, "http://localhost:9000")
        monkeypatch.delenv(NOTIFIER_API_KEY_ENV, raising=False)
        monkeypatch.setenv(NOTIFIER_ENABLED_ENV, "1")

        with pytest.raises(RuntimeError, match=NOTIFIER_API_KEY_ENV):
            get_notifier_client()

    def test_url_key_and_flag_together_build_a_client(self, monkeypatch) -> None:
        monkeypatch.setenv(NOTIFIER_BASE_URL_ENV, "http://localhost:9000")
        monkeypatch.setenv(NOTIFIER_API_KEY_ENV, "nk_test")
        monkeypatch.setenv(NOTIFIER_ENABLED_ENV, "1")

        assert get_notifier_client() is not None


class TestEnvironmentNotifierGate:
    """The loud half: a URL without the flag is a misconfiguration, not a mode.

    ``get_notifier_client`` already fails closed, but closed-and-silent trades
    one production hazard for another — drop the line from the unit and Watcher
    stops notifying, with nothing but a per-dispatch error to say so. The one
    combination that is always a mistake in either direction refuses to start,
    exactly as #262 decided for the bus.
    """

    def test_url_without_the_flag_refuses(self) -> None:
        with pytest.raises(NotifierNotEnabled) as excinfo:
            assert_environment_notifier_allowed(
                {NOTIFIER_BASE_URL_ENV: "http://localhost:9000"},
            )
        message = str(excinfo.value)
        assert NOTIFIER_ENABLED_ENV in message
        assert "deploy/watcher.service" in message
        assert "scripts/dev_server.sh" in message

    def test_url_with_a_non_opt_in_value_refuses(self) -> None:
        with pytest.raises(NotifierNotEnabled):
            assert_environment_notifier_allowed(
                {NOTIFIER_BASE_URL_ENV: "http://localhost:9000", NOTIFIER_ENABLED_ENV: "true"},
            )

    def test_url_with_the_flag_is_allowed(self) -> None:
        assert_environment_notifier_allowed(
            {NOTIFIER_BASE_URL_ENV: "http://localhost:9000", NOTIFIER_ENABLED_ENV: "1"},
        )

    def test_no_url_is_allowed_flag_or_not(self) -> None:
        """No URL names no notifier, so nothing can be dispatched by accident.

        Making the absence fatal would stop every dev server, script and test
        run that never wanted one — and the client's own refusal already covers
        the case where something tries to dispatch anyway.
        """
        assert_environment_notifier_allowed({})
        assert_environment_notifier_allowed({NOTIFIER_ENABLED_ENV: "1"})
