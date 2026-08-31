"""The test session must never be able to reach the production notifier.

Third sibling of ``test_db_isolation`` and ``test_bus_isolation``, and the last
outbound production credential to get a gate (#277).

``/etc/watcher/.env`` *carried* ``WATCHER_NOTIFIER_BASE_URL`` and
``WATCHER_NOTIFIER_API_KEY`` until #278 moved them to a unit-only file, and
AGENTS.md tells every agent to ``source scripts/load-env.sh`` before running
pytest. Before this, ``get_notifier_client()`` read both straight from
``os.environ`` and raised only when they were *unset* — so a suite, a hand-run
dev server, a script or a REPL in a prod-sourced shell dispatched for real, to
the production notifier, as the production tenant, and the dispatch *succeeded
silently*.

Two mechanisms, deliberately, because they cover different things:

* The **scrub** below makes "no notifier" the default for pytest specifically.
  A test that wants one sets the variables via ``monkeypatch.setenv``, which
  restores itself on teardown.
* The **gate** (``WATCHER_NOTIFIER_ENABLED=1``) covers every other launch path at once,
  which a scrub cannot: a scrub has to be written once per entry point, and
  nothing scrubs a ``python -c``, an agent shell, or ``scripts/*.py``.

The gate is the load-bearing half. When #277 was filed the whole suite already
passed with the base URL pointed at a black hole — no test reached the real
client — so the residue rows that prompted it came from a hand-run path the
scrub would never have covered.

#278 then removed the credential from the shell environment entirely
(``/etc/watcher/notifier.env``, loaded by the unit alone), which is a third
mechanism and not a replacement for either: the scrub keeps this suite's
guarantee a property of the suite rather than of the VM's file layout, and the
gate still covers the paths neither reaches. See
``tests/deploy/test_notifier_credential_is_unit_only.py``.
"""

import os
import pathlib
import re

import pytest

from src.core.notifier_client import (
    WATCHER_NOTIFIER_API_KEY_ENV,
    WATCHER_NOTIFIER_BASE_URL_ENV,
    WATCHER_NOTIFIER_ENABLED_ENV,
    NotifierCredentialMissing,
    NotifierNotEnabled,
    assert_environment_notifier_allowed,
    get_notifier_client,
    notifier_enabled,
)


class TestNotifierIsolation:
    def test_notifier_env_is_cleared_for_the_session(self) -> None:
        assert os.environ.get(WATCHER_NOTIFIER_BASE_URL_ENV) is None
        assert os.environ.get(WATCHER_NOTIFIER_API_KEY_ENV) is None

    def test_the_gate_flag_is_cleared_too(self) -> None:
        """The flag is scrubbed beside the pair it gates (#278).

        Since #278 the flag held without a URL is itself a startup failure, so
        an exported ``WATCHER_NOTIFIER_ENABLED=1`` — a shell that copied the
        unit's line, an agent that ran an experiment — would make every test
        touching the lifespan fail on the environment rather than the code.
        Clearing it keeps the session's answer to "is there a notifier?" a flat
        no, whatever the launching shell believed.
        """
        assert os.environ.get(WATCHER_NOTIFIER_ENABLED_ENV) is None

    def test_the_dev_pair_is_cleared_too(self) -> None:
        """The scratch-notifier pair is scrubbed beside the production one (CR-3).

        ``scripts/dev_server.sh`` reads ``WATCHER_DEV_NOTIFIER_BASE_URL`` and
        ``WATCHER_DEV_NOTIFIER_API_KEY`` from the repo ``.env`` — which
        ``scripts/load-env.sh`` exports before every pytest run. Nothing in
        ``src/`` reads either today, and the flag being cleared means no client
        could be built from them regardless; this is the same defence-in-depth
        the bus block four lines above already applies to
        ``WATCHER_DEV_BUS_REDIS_URL``.

        It stops being hypothetical the moment notifier mints the development
        key #278 asked for: that key belongs in the repo ``.env``, and from that
        day a test session would carry a live notifier address and credential
        while this module's docstring claims the session has none.

        Names spelled literally, not imported: nothing in ``src/`` reads
        either variable — they are ``scripts/dev_server.sh``'s alone — so
        exporting constants for them would advertise readers that do not
        exist. Same shape as ``tests/test_bus_isolation.py``, which spells
        ``WATCHER_DEV_BUS_REDIS_URL`` the same way and for the same reason.
        """
        assert os.environ.get("WATCHER_DEV_NOTIFIER_BASE_URL") is None
        assert os.environ.get("WATCHER_DEV_NOTIFIER_API_KEY") is None

    def test_client_construction_raises_rather_than_connecting(self) -> None:
        """The path any unpatched test takes: a refusal, not a dispatch."""
        with pytest.raises(RuntimeError, match=f"{WATCHER_NOTIFIER_BASE_URL_ENV} .* is required"):
            get_notifier_client()

    def test_use_remote_notify_is_not_scrubbed_because_nothing_reads_it(self) -> None:
        """#277 proposed scrubbing three variables; only two are real.

        ``USE_REMOTE_NOTIFY`` has had no reader in ``src/`` since the local
        Apprise path was removed — see
        ``tests/core/notifications/test_notify_remote_only.py``, which asserts
        it is *ignored*. Scrubbing it would advertise a switch that does not
        exist. This pins the omission as deliberate, and fails the moment a
        reader comes back — at which point the scrub, not this test, is the fix.

        Sweeps the *source text* of all of ``src/``. Two earlier shapes were
        both too narrow: ``__dict__`` holds only top-level bindings, so a read
        buried in a function body sailed through it (CR-2); a hand-maintained
        list of two modules then missed any third file that reacquired the
        variable (CR-9). A whole-tree sweep has neither hole and needs no
        upkeep when modules move — which also retires the CWD-relative paths
        that made the previous version fail from anywhere but the repo root
        (CR-8).
        """
        src_root = pathlib.Path(__file__).resolve().parents[1] / "src"
        assert src_root.is_dir(), f"{src_root} is missing — this guard is anchored wrong"

        offenders = [
            path.relative_to(src_root.parent)
            for path in sorted(src_root.rglob("*.py"))
            if "USE_REMOTE_NOTIFY" in path.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            f"USE_REMOTE_NOTIFY is read again in: {', '.join(map(str, offenders))}. "
            "If that is deliberate, add it to the tests/conftest.py scrub and "
            "delete this test — the omission it pins is no longer true."
        )


class TestNotifierEnabledGate:
    """#277: a URL is not permission — ``WATCHER_NOTIFIER_ENABLED=1`` is.

    The same rule ``src.core.bus`` applies to the broker address (#262) and
    ``src.core.db_safety`` applies to the production database (#233). Only the
    exact string ``"1"`` opts in; a fuzzy truthiness check would let a stray
    value quietly re-open the hole.
    """

    def test_exact_one_opts_in(self) -> None:
        assert notifier_enabled({WATCHER_NOTIFIER_ENABLED_ENV: "1"}) is True

    @pytest.mark.parametrize("value", ["true", "yes", "0", "", "1 "])
    def test_anything_else_does_not(self, value: str) -> None:
        assert notifier_enabled({WATCHER_NOTIFIER_ENABLED_ENV: value}) is False

    def test_absent_does_not(self) -> None:
        assert notifier_enabled({}) is False

    def test_client_refuses_a_url_held_without_the_flag(self, monkeypatch) -> None:
        monkeypatch.setenv(WATCHER_NOTIFIER_BASE_URL_ENV, "http://notifier.invalid:9000")
        monkeypatch.setenv(WATCHER_NOTIFIER_API_KEY_ENV, "nk_test")
        monkeypatch.delenv(WATCHER_NOTIFIER_ENABLED_ENV, raising=False)

        with pytest.raises(NotifierNotEnabled) as excinfo:
            get_notifier_client()
        assert WATCHER_NOTIFIER_ENABLED_ENV in str(excinfo.value)

    def test_missing_url_is_still_the_url_error(self, monkeypatch) -> None:
        """The operator reads the variable that is actually missing.

        Same ordering rule as ``bus_disabled_reason``: reporting the flag first
        would send someone hunting for an opt-in when the real problem is that
        no notifier was configured at all.

        Deliberately close to
        ``tests/core/test_notifier_client.py::TestGetNotifierClient`` (CR-4).
        That module owns the factory's *contract*; this one owns its ordering
        **relative to the gate**, which is why the flag is set here and absent
        there. Change one, check the other.
        """
        monkeypatch.delenv(WATCHER_NOTIFIER_BASE_URL_ENV, raising=False)
        monkeypatch.setenv(WATCHER_NOTIFIER_ENABLED_ENV, "1")

        with pytest.raises(RuntimeError, match=WATCHER_NOTIFIER_BASE_URL_ENV):
            get_notifier_client()

    def test_missing_key_is_still_the_key_error(self, monkeypatch) -> None:
        """Sibling of the test above; same relationship to
        ``tests/core/test_notifier_client.py`` (CR-4)."""
        monkeypatch.setenv(WATCHER_NOTIFIER_BASE_URL_ENV, "http://notifier.invalid:9000")
        monkeypatch.delenv(WATCHER_NOTIFIER_API_KEY_ENV, raising=False)
        monkeypatch.setenv(WATCHER_NOTIFIER_ENABLED_ENV, "1")

        with pytest.raises(RuntimeError, match=WATCHER_NOTIFIER_API_KEY_ENV):
            get_notifier_client()


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
                {WATCHER_NOTIFIER_BASE_URL_ENV: "http://notifier.invalid:9000"},
            )
        message = str(excinfo.value)
        assert WATCHER_NOTIFIER_ENABLED_ENV in message
        assert "deploy/watcher.service" in message
        assert "scripts/dev_server.sh" in message

    def test_url_with_a_non_opt_in_value_refuses(self) -> None:
        with pytest.raises(NotifierNotEnabled):
            assert_environment_notifier_allowed(
                {
                    WATCHER_NOTIFIER_BASE_URL_ENV: "http://notifier.invalid:9000",
                    WATCHER_NOTIFIER_ENABLED_ENV: "true",
                },
            )

    def test_url_with_the_flag_is_allowed(self) -> None:
        assert_environment_notifier_allowed(
            {
                WATCHER_NOTIFIER_BASE_URL_ENV: "http://notifier.invalid:9000",
                WATCHER_NOTIFIER_ENABLED_ENV: "1",
            },
        )

    def test_no_url_and_no_flag_is_allowed(self) -> None:
        """No URL names no notifier, so nothing can be dispatched by accident.

        Making the absence fatal would stop every dev server, script and test
        run that never wanted one — and the client's own refusal already covers
        the case where something tries to dispatch anyway.
        """
        assert_environment_notifier_allowed({})

    def test_the_flag_without_a_url_refuses(self) -> None:
        """#278's other direction: opted in, and nothing to opt in *to*.

        Only ``deploy/watcher.service`` sets the flag, and since #278 the
        credential it goes with lives in ``/etc/watcher/notifier.env`` — a
        separate file, loaded by that unit alone. So the flag without a URL
        means the service came up without its credential file, and would run
        silently un-notifying: every dispatch failing one at a time in the
        worker log, with a green ``systemctl status``.

        ``EnvironmentFile=`` without the ``-`` prefix already fails that start.
        This is the in-app half, and it covers what systemd cannot see — a file
        that exists but was truncated, emptied, or had the assignment renamed.

        Nothing else can hit it: ``scripts/dev_server.sh`` sets both or neither,
        and ``tests/conftest.py`` clears all three.
        """
        with pytest.raises(NotifierCredentialMissing) as excinfo:
            assert_environment_notifier_allowed({WATCHER_NOTIFIER_ENABLED_ENV: "1"})
        message = str(excinfo.value)
        assert WATCHER_NOTIFIER_BASE_URL_ENV in message
        assert "/etc/watcher/notifier.env" in message


#: Roots swept for a notifier URL. ``docs/`` is deliberately absent: the docs
#: name the real host on purpose, which is their job.
SWEPT_ROOTS = ("tests", "src", "scripts")

#: Suffixes swept within those roots. ``.sh`` is not decoration (CR-12): adding
#: ``scripts/`` while globbing ``*.py`` excluded ``scripts/dev_server.sh``, the
#: one file in the tree that reads and rewrites ``WATCHER_NOTIFIER_BASE_URL`` —
#: the highest-risk file in the newly added root, invisible to the guard that
#: claimed to cover it.
SWEPT_SUFFIXES = (".py", ".sh")

#: Port-keyed, not semantic. Nothing in a URL says "notifier", so the pair of
#: ports notifier serves is the identifying signal — which means a notifier URL
#: written on some other port slips through, and an unrelated stub server on
#: 9000 would be reported with a notifier's message. Both are acceptable: the
#: ports are convention here, and the guard is a tripwire, not a type system.
NOTIFIER_URL = re.compile(r"""https?://([^"'\s/]+):900[01]""")


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _swept_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Every file under ``SWEPT_ROOTS`` whose suffix is swept, sorted.

    Selection is a function so it can be asserted directly: the trees are
    clean, so the sweep below proves nothing about *what it opened*, which is
    exactly how ``scripts/*.sh`` sat outside a guard that named ``scripts/``.
    """
    files: list[pathlib.Path] = []
    for root in SWEPT_ROOTS:
        tree = repo_root / root
        assert tree.is_dir(), f"{tree} is missing — this guard is anchored wrong"
        files += [
            path for path in tree.rglob("*") if path.is_file() and path.suffix in SWEPT_SUFFIXES
        ]
    return sorted(files)


def _resolvable_notifier_hosts(text: str) -> list[tuple[int, str]]:
    """Return ``(line, host)`` for every notifier URL naming a host that resolves.

    A host under ``.invalid`` is reserved by RFC 2606 to never resolve, so it
    is the only one that cannot become stale. Everything else — loopback, the
    tailnet name, some future VM — is a fact about this quarter's deployment
    that a fixture has no business restating.
    """
    return [
        (text.count("\n", 0, match.start()) + 1, match.group(1))
        for match in NOTIFIER_URL.finditer(text)
        if not match.group(1).endswith(".invalid")
    ]


def _fixture_url(host: str, port: int = 9000) -> str:
    """Build a URL at call time so this module holds no literal to trip on.

    The sweep below reads its own source. Spelling a resolvable host inline —
    even inside a fixture proving the guard reports it — makes this file its
    own first offender, which is not a hypothetical: it happened on the way in.
    """
    return f"http://{host}:{port}"


class TestResolvableNotifierHosts:
    """The selection is the guard's real contract, so it is pinned directly.

    Same reasoning as ``TestCredentialCandidates`` in
    ``tests/deploy/test_notifier_credential_is_unit_only.py``: the sweep below
    can only assert about a tree that is (correctly) clean, so on a green
    checkout it exercises no selection logic at all.
    """

    def test_loopback_is_reported(self) -> None:
        assert _resolvable_notifier_hosts(_fixture_url("localhost")) == [(1, "localhost")]

    def test_the_real_host_is_reported_too(self) -> None:
        """The repair that looks obvious and is not: a live hostname is exactly
        what goes stale on the next move."""
        assert _resolvable_notifier_hosts(_fixture_url("notifier")) == [(1, "notifier")]

    def test_a_reserved_name_is_not_reported(self) -> None:
        assert _resolvable_notifier_hosts(_fixture_url("notifier.invalid", 9001)) == []

    def test_the_line_number_is_the_match_line(self) -> None:
        assert _resolvable_notifier_hosts("a\nb\n" + _fixture_url("localhost", 9001)) == [
            (3, "localhost")
        ]

    def test_other_ports_are_out_of_scope(self) -> None:
        """Port-keyed by construction — see NOTIFIER_URL."""
        assert _resolvable_notifier_hosts(_fixture_url("localhost", 5432)) == []

    def test_a_quoted_url_is_matched_without_its_quote(self) -> None:
        """The character class exists to stop the host swallowing the delimiter."""
        assert _resolvable_notifier_hosts('"' + _fixture_url("localhost") + '"') == [
            (1, "localhost")
        ]


class TestSweptTreesNameNoResolvableHost:
    """No source, script or fixture may name a notifier host that could answer (#280).

    Fixtures do not open a socket — the URL is arbitrary, and that is exactly
    why it drifts. Naming loopback on notifier's port was true while notifier
    ran beside watcher; notifier#43 moved it to its own VM and the literal
    stopped describing anything, while still reading as the real production
    URL. The obvious repair — substitute the new host — is worse: it makes the
    suite track production configuration, so the next move breaks these files
    again, and a fixture holding the live URL is *more* copyable, not less,
    which is the whole hazard.

    ``src/`` and ``scripts/`` are swept beside ``tests/`` (#280 CR-5) for a
    different reason: there a literal would be a real connection target, not an
    inert fixture. The base URL is configuration and arrives from the
    environment — a hardcoded one is the defect, whatever host it names. Shell
    counts as source in ``scripts/`` (CR-12) — the notifier-aware file there is
    ``dev_server.sh``, and a Python-only glob never opened it.
    """

    def test_shell_scripts_are_swept_beside_python(self) -> None:
        """CR-12, pinned by name: the file the widening was for.

        ``scripts/dev_server.sh`` is where a notifier URL would do damage, and a
        ``*.py`` glob over ``scripts/`` walks straight past it. Asserting the
        selection rather than the result, because the tree is (correctly) clean
        — a sweep that opens nothing passes just as loudly as one that works.
        """
        swept = {path.name for path in _swept_files(REPO_ROOT)}
        assert "dev_server.sh" in swept
        assert "load-env.sh" in swept
        assert "test_notifier_isolation.py" in swept

    def test_docs_are_not_swept(self) -> None:
        """They name the real host on purpose."""
        assert not [p for p in _swept_files(REPO_ROOT) if "docs/" in str(p)]

    def test_no_swept_file_names_a_resolvable_notifier_host(self) -> None:
        offenders: list[str] = []
        for path in _swept_files(REPO_ROOT):
            text = path.read_text(encoding="utf-8")
            offenders += [
                f"{path.relative_to(REPO_ROOT)}:{line} \u2192 {host}"
                for line, host in _resolvable_notifier_hosts(text)
            ]

        assert not offenders, (
            "these name a notifier host that resolves:\n  "
            + "\n  ".join(offenders)
            + "\nUse a reserved name that never resolves (notifier.invalid) rather "
            "than whichever host is real this quarter."
        )
