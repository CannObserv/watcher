"""Regression guard: co-core resolves from the co-pypi index, and only from it (#358).

``co-core`` and ``co-core-aio`` are private: they are in the co-pypi Artifact
Registry index and nowhere public. cannobserv's RELEASING.md → *Path C* states
the rules a consumer keeps, and each is a test here, because each one breaks
quietly:

* **The URL is the org registry's byte for byte**, trailing slash included.
  Dependabot's updater matches it to the org's configured registry by string;
  a near miss sends it without credentials.
* **``explicit = true``, plus a ``[tool.uv.sources]`` entry per co-core name.**
  An explicit index serves only the names pinned to it, so co-core never comes
  from PyPI, where the names are unclaimed (the dependency-confusion shape),
  and nothing else comes from co-pypi.
* **Every co-core package is declared directly.** uv binds a source to a direct
  dependency only; an undeclared one, reached transitively, goes to PyPI.
* **The lock carries each file's sha256.** That is what retired #393's digest
  table: ``uv sync`` refuses a file that misses the lock's hash.

Pure file reads: no network, no database.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
_LOCK = tomllib.loads((_REPO_ROOT / "uv.lock").read_text())

INDEX = "co-pypi"
URL = "https://us-west1-python.pkg.dev/co-gcs/co-pypi/simple/"
HOST = "us-west1-python.pkg.dev"


def _is_co_core(name: str) -> bool:
    return canonicalize_name(name).startswith("co-core")


def _locked_co_core() -> list[dict]:
    return [p for p in _LOCK["package"] if _is_co_core(p["name"])]


def _uv() -> dict:
    return _PYPROJECT["tool"]["uv"]


class TestTheIndex:
    def test_one_co_pypi_index_at_the_org_registrys_url(self):
        indexes = [i for i in _uv().get("index", []) if i.get("name") == INDEX]
        assert len(indexes) == 1, f"want one [[tool.uv.index]] named {INDEX!r}"
        assert indexes[0]["url"] == URL, "byte for byte, trailing slash included"

    def test_the_index_is_explicit(self):
        [index] = [i for i in _uv()["index"] if i["name"] == INDEX]
        assert index.get("explicit") is True, (
            "an implicit index would serve every name, and co-core would be one"
            " lookup away from PyPI's unclaimed namesake"
        )

    def test_no_find_links_and_no_other_index(self):
        assert "find-links" not in _uv(), "#358 retired the wheelhouse; one source per name"
        assert [i["name"] for i in _uv()["index"]] == [INDEX]


class TestEveryCoCorePackage:
    def test_the_sweep_finds_both(self):
        """Guard the guard: a rename would leave every test below vacuous."""
        assert {p["name"] for p in _locked_co_core()} == {"co-core", "co-core-aio"}

    @pytest.mark.parametrize("package", _locked_co_core(), ids=lambda p: p["name"])
    def test_is_declared_directly(self, package: dict):
        declared = {
            canonicalize_name(Requirement(r).name) for r in _PYPROJECT["project"]["dependencies"]
        }
        assert canonicalize_name(package["name"]) in declared, (
            f"{package['name']} is locked but not declared: uv binds sources to direct"
            " dependencies only, so a transitive co-core resolves from PyPI"
        )

    @pytest.mark.parametrize("package", _locked_co_core(), ids=lambda p: p["name"])
    def test_is_pinned_to_the_index(self, package: dict):
        assert _uv()["sources"][package["name"]] == {"index": INDEX}

    @pytest.mark.parametrize("package", _locked_co_core(), ids=lambda p: p["name"])
    def test_locks_from_the_index_with_a_sha256_per_file(self, package: dict):
        assert package["source"] == {"registry": URL}
        files = [package["sdist"], *package["wheels"]]
        assert len(files) == 2, "one sdist and one wheel, as the release publishes"
        for dist in files:
            assert dist["url"].startswith(f"https://{HOST}/"), dist["url"]
            assert dist["hash"].startswith("sha256:") and len(dist["hash"]) == 7 + 64


def test_nothing_else_resolves_from_the_index():
    others = [
        p["name"]
        for p in _LOCK["package"]
        if not _is_co_core(p["name"]) and p.get("source", {}).get("registry") == URL
    ]
    assert others == []


class TestTheDigestTableAgrees:
    """One-shot proof for the swap (#358): the index's sha256 for each 0.37.0
    file equals #393's ``[tool.watcher.wheelhouse-sha256]`` pin. It goes with
    the table, in the commit that deletes both."""

    def test_the_lock_hashes_equal_the_table(self):
        table = _PYPROJECT["tool"]["watcher"]["wheelhouse-sha256"]
        locked = {
            dist["url"].rsplit("/", 1)[1]: dist["hash"].removeprefix("sha256:")
            for package in _locked_co_core()
            for dist in [package["sdist"], *package["wheels"]]
        }
        assert len(table) == 4
        assert locked == table
