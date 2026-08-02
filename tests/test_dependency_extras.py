"""Regression guard: every extra requested in pyproject.toml must exist.

``uv`` reports an unknown extra as a *warning* during lock, not an error, so a
misspelled or removed extra resolves to nothing and the manifest silently stops
declaring what it claims to (#242 — ``procrastinate[psycopg]`` named an extra
procrastinate never published; the psycopg driver was arriving via the base
requirement instead).

These tests need no database — they compare the requested extras in
``pyproject.toml`` against each distribution's published ``Provides-Extra``
metadata in the resolved environment.

**Precondition:** the environment is synced from ``uv.lock``. The comparison is
against *installed* metadata, so a venv holding a different version than the
lock answers about the wrong distribution in either direction. CI syncs before
running (``uv sync --group dev``); locally, run ``uv sync`` after editing
dependencies.

**Not covered, deliberately:** the inverse risk — a dependency dropping a
package we relied on it to pull in — needs no guard here. procrastinate's
``psycopg_connector`` binds ``pool_factory=psycopg_pool.AsyncConnectionPool`` as
a default argument, evaluated at module import, and its ``import_or_wrapper``
substitutes an object whose ``__getattr__`` re-raises the ``ImportError``. A
missing psycopg therefore breaks ``import procrastinate`` outright and reddens
the whole suite — it cannot fail quietly at worker startup.
"""

import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _declared_requirements() -> list[Requirement]:
    """Every requirement in project.dependencies, optional-dependencies, groups.

    ``dependency-groups`` entries may be ``{include-group = "..."}`` tables
    (PEP 735) rather than requirement strings; those are skipped — the included
    group is itself a key in the same table, so nothing is missed.
    """
    data = tomllib.loads(_PYPROJECT.read_text())
    project = data["project"]
    raw: list[str] = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        raw.extend(extra_deps)
    for group in data.get("dependency-groups", {}).values():
        raw.extend(entry for entry in group if isinstance(entry, str))
    return [Requirement(spec) for spec in raw]


def _requirements_with_extras() -> list[Requirement]:
    return [req for req in _declared_requirements() if req.extras]


def _published_extras(dist_name: str) -> set[str]:
    """Canonicalized ``Provides-Extra`` names for an installed distribution."""
    provided = metadata.metadata(dist_name).get_all("Provides-Extra") or []
    return {canonicalize_name(extra) for extra in provided}


class TestRequestedExtrasExist:
    def test_pyproject_requirements_parse(self):
        """Guard the guard — a parser returning nothing makes the sweep vacuous.

        Asserts on the parse, not on extras existing: dropping the last
        bracketed extra is a legitimate dependency change and must not redden
        the suite, whereas a restructured manifest or a broken parser must.
        """
        assert _declared_requirements(), "parsed no requirements from pyproject.toml"

    @pytest.mark.parametrize(
        "requirement",
        _requirements_with_extras(),
        ids=lambda req: f"{req.name}[{','.join(sorted(req.extras))}]",
    )
    def test_requested_extras_are_published(self, requirement: Requirement):
        """Each bracketed extra is one the installed distribution actually has."""
        try:
            published = _published_extras(requirement.name)
        except metadata.PackageNotFoundError:  # pragma: no cover - env not synced
            pytest.skip(f"{requirement.name} not installed; run `uv sync`")

        requested = {canonicalize_name(extra) for extra in requirement.extras}
        unknown = requested - published
        assert not unknown, (
            f"{requirement.name} does not publish extra(s) {sorted(unknown)}; "
            f"available: {sorted(published) or '(none)'}. "
            "An unknown extra resolves to nothing — drop it or use the real name."
        )
