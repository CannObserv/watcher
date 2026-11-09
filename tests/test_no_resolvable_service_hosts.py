"""No source, script or fixture names a resolvable host on notifier's ports (#280).

Moved out of ``test_notifier_isolation.py`` when #379 deleted the notifier
client: the rest of that file guarded a credential nothing reads any more, but
this sweep still guards live code. ``src/ops/checkin.py`` is a client of
co-status, which kept notifier's check-in contract and its port pair (#330), and
its rule — a service URL in ``src/`` is configuration, so a literal is the
defect whatever host it names — is this sweep's.
"""

import pathlib
import re

#: Roots swept for a notifier URL. ``docs/`` is deliberately absent: the docs
#: name the real host on purpose, which is their job.
SWEPT_ROOTS = ("tests", "src", "scripts")

#: Suffixes swept within those roots. ``.sh`` is not decoration (CR-12): adding
#: ``scripts/`` while globbing ``*.py`` excluded ``scripts/dev_server.sh``, then
#: the one file in the tree that rewrote the notifier's base URL — the
#: highest-risk file in the newly added root, invisible to the guard that
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

    The sweep below can only assert about a tree that is (correctly) clean, so
    on a green checkout it exercises no selection logic at all.
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
    counts as source in ``scripts/`` (CR-12) — the notifier-aware file there was
    ``dev_server.sh``, and a Python-only glob never opened it.
    """

    def test_shell_scripts_are_swept_beside_python(self) -> None:
        """CR-12, pinned by name: the file the widening was for.

        ``scripts/dev_server.sh`` was where a notifier URL would do damage, and a
        ``*.py`` glob over ``scripts/`` walks straight past it. Asserting the
        selection rather than the result, because the tree is (correctly) clean
        — a sweep that opens nothing passes just as loudly as one that works.
        """
        swept = {path.name for path in _swept_files(REPO_ROOT)}
        assert "dev_server.sh" in swept
        assert "load-env.sh" in swept
        assert "test_no_resolvable_service_hosts.py" in swept

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
