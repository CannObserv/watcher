"""Regression guard: every git source in pyproject.toml installs anywhere.

``notifier-client`` was pinned through ``ssh://git@github-notifier/...`` — an
SSH host alias that exists only on the watcher VM — so CI had to
``git config url.insteadOf``-rewrite it back to HTTPS in every job before it
could install at all (#284). ``CannObserv/notifier`` is public; the alias bought
nothing, and a rewrite means CI resolves a URL other than the one the VM does.

A git source is also pinned by ``tag``: notifier's release procedure makes the
tag the pin (adopt a release by changing it, stay put by doing nothing), and uv
enforces no version floor against a git source. A ``branch`` floats; a ``rev``
hides which release is in use behind a sha.

These tests need no database and no network — they read ``pyproject.toml``,
``uv.lock`` and ``.github/workflows/ci.yml`` as text.
"""

import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_LOCK = _REPO_ROOT / "uv.lock"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _git_source_entries(manifest: dict) -> list[tuple[str, dict]]:
    """``[tool.uv.sources]`` entries that name a ``git`` URL, as ``(package, entry)``.

    uv accepts either one table or a list of marker-scoped tables per package.
    Every git entry of a list is returned, so a package may appear more than
    once; a package whose every entry is non-git (path, index, url) is absent.
    """
    sources = manifest.get("tool", {}).get("uv", {}).get("sources", {})
    return [
        (name, entry)
        for name, spec in sources.items()
        for entry in (spec if isinstance(spec, list) else [spec])
        if "git" in entry
    ]


def _manifest_git_sources() -> list[tuple[str, dict]]:
    """Every git source entry in this repo's ``pyproject.toml``."""
    return _git_source_entries(tomllib.loads(_PYPROJECT.read_text()))


def _locked_git_packages() -> set[str]:
    """Names of every package ``uv.lock`` resolved from a git source."""
    packages = tomllib.loads(_LOCK.read_text()).get("package", [])
    return {pkg["name"] for pkg in packages if "git" in pkg.get("source", {})}


class TestGitSources:
    def test_sweep_matches_the_lock(self):
        """Guard the guard — the manifest sweep sees every git package the lock holds.

        Compared against the lock rather than asserting a count, so graduating
        ``notifier-client`` to a published package (both sides empty) stays
        green while a parser that finds nothing does not.
        """
        assert {name for name, _ in _manifest_git_sources()} == _locked_git_packages(), (
            "pyproject.toml's git sources and uv.lock's git packages disagree — "
            "run `uv lock`, or fix _git_source_entries if it stopped finding them."
        )

    def test_sweep_keeps_every_entry_of_a_marker_scoped_list(self):
        """Guard the guard — a list-form source is swept entry by entry.

        The real manifest has no list-form source, and the lock comparison
        above sees package names only, so nothing else here would notice a
        sweep that kept one entry per package: an SSH alias scoped to one
        platform would hide behind an HTTPS entry for another.
        """
        manifest = tomllib.loads(
            """
            [tool.uv.sources]
            pkg = [
              { git = "ssh://git@alias/pkg.git", tag = "v1", marker = "os_name == 'posix'" },
              { git = "https://x.test/pkg.git", tag = "v1", marker = "os_name == 'nt'" },
            ]
            """
        )
        assert [entry["git"] for _, entry in _git_source_entries(manifest)] == [
            "ssh://git@alias/pkg.git",
            "https://x.test/pkg.git",
        ]

    @pytest.mark.parametrize(
        ("name", "entry"), _manifest_git_sources(), ids=[n for n, _ in _manifest_git_sources()]
    )
    def test_git_source_is_public_https(self, name: str, entry: dict):
        """An SSH host alias resolves on one machine; HTTPS resolves everywhere."""
        url = entry["git"]
        assert url.startswith("https://"), (
            f"{name} is sourced from {url!r}. Use its public https:// URL — an "
            "SSH host alias exists only on the machine that defines it (#284)."
        )

    @pytest.mark.parametrize(
        ("name", "entry"), _manifest_git_sources(), ids=[n for n, _ in _manifest_git_sources()]
    )
    def test_git_source_pins_a_tag(self, name: str, entry: dict):
        """The tag is the pin: a branch floats, and a rev hides the release."""
        assert "tag" in entry and not {"branch", "rev"} & entry.keys(), (
            f"{name}'s git source must pin a release `tag` and nothing else; got "
            f"{sorted(entry.keys() - {'git', 'subdirectory'})} (#284)."
        )


def test_ci_rewrites_no_git_url():
    """CI installs the manifest as written, so green CI means the VM resolves it too.

    A ``url.<base>.insteadOf`` rewrite is how CI worked around the SSH alias;
    with every source on public HTTPS it has nothing left to rewrite.
    """
    assert "insteadOf" not in _CI_WORKFLOW.read_text(), (
        "ci.yml rewrites a git URL. Fix the source in pyproject.toml instead — "
        "a rewrite makes CI install something the VM does not (#284)."
    )
