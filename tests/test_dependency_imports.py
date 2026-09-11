"""Regression guard: every third-party module this repo imports is declared.

Six modules were imported under ``src/`` while reaching the environment only
transitively (#294). Their absence from the manifest could not fail quietly —
all six load on ``import src.api.main``, so a dependency dropping one reddens
collection, the argument ``tests/test_dependency_extras.py`` makes for psycopg.
What it cost was the **bound**: an undeclared module is capped, or not, by
whichever dependency pulls it in. ``pydantic``, which every schema here is
written against, answered to co-core's range — FastAPI's has no cap; ``redis``
answered to co-core-aio's cap rather than to the redis-py version
``docs/BUS-CONNECTION-POLICY.md`` was measured on.

**Resolution is by dotted prefix, not top-level name.** ``google`` is a
namespace several distributions ship into, so asking which distribution
provides ``google`` passes ``google.api_core`` whenever any of them —
google-cloud-storage — is declared. Each import resolves to the distributions
shipping its longest dotted prefix, read from their installed file lists.

**Scope:** ``src/`` and ``alembic/`` against ``[project.dependencies]``;
``tests/`` against those plus the ``dev`` group. ``scripts/`` is not swept —
its scripts run under ``uv run --no-project`` with their own ``--with``.

**Precondition:** the environment is synced from ``uv.lock``, as for
``tests/test_dependency_extras.py``.
"""

import ast
import sys
import tomllib
from collections import defaultdict
from functools import cache
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
# This repo's own top-level packages. `alembic/` is not one: `from alembic
# import op` there names the declared library.
_FIRST_PARTY = frozenset({"src", "tests"})

# Imported directly, but deliberately left to arrive transitively:
# distribution -> (the distribution that must keep providing it, why).
_TRANSITIVE_BY_DESIGN: dict[str, tuple[str, str]] = {
    "croniter": (
        "procrastinate",
        "src/workers/watch_status.py validates the republish cron with the parser "
        "procrastinate evaluates it with. Declared, croniter would outlive procrastinate "
        "swapping parsers and keep approving expressions for a parser nothing uses.",
    ),
}


def _requirement_names(specs: list) -> frozenset[str]:
    """Canonical names of the requirement strings in *specs*.

    PEP 735 ``{include-group = "..."}`` tables are skipped; the included group
    is itself a key of ``dependency-groups``.
    """
    return frozenset(
        canonicalize_name(Requirement(spec).name) for spec in specs if isinstance(spec, str)
    )


_MANIFEST = tomllib.loads(_PYPROJECT.read_text())
_RUNTIME = _requirement_names(_MANIFEST["project"]["dependencies"])
_WITH_DEV = _RUNTIME | _requirement_names(_MANIFEST["dependency-groups"]["dev"])
# (directories swept, the distributions an import there may resolve to)
_SCOPES = {
    "src+alembic": (("src", "alembic"), _RUNTIME),
    "tests": (("tests",), _WITH_DEV),
}


def _module_name(file: metadata.PackagePath) -> str | None:
    """The dotted module a distribution's ``.py`` file defines; None for any other file.

    Rejects every path with a part that is not an identifier — ``*.dist-info``,
    ``*.data``, and the ``..`` of an installed script. An extension module
    (``.so``) is not mapped: importing one directly reports it as having no
    installed distribution — loud, and nothing here does.
    """
    if file.suffix != ".py":
        return None
    *directories, name = file.parts
    stem = name.removesuffix(".py")
    parts = directories if stem == "__init__" else [*directories, stem]
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


@cache
def _module_owners() -> dict[str, frozenset[str]]:
    """Each installed module path, and every package above it, to the distributions shipping it."""
    owners: dict[str, set[str]] = defaultdict(set)
    for dist in metadata.distributions():
        name = canonicalize_name(dist.metadata["Name"])
        for file in dist.files or ():
            module = _module_name(file)
            if module is None:
                continue
            parts = module.split(".")
            for end in range(1, len(parts) + 1):
                owners[".".join(parts[:end])].add(name)
    return {module: frozenset(names) for module, names in owners.items()}


def _owning_distributions(module: str) -> frozenset[str]:
    """The distributions shipping the longest dotted prefix of *module* that any ships."""
    owners = _module_owners()
    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        found = owners.get(".".join(parts[:end]))
        if found:
            return found
    return frozenset()


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    """``(dotted name, line)`` for each absolute import in *path*.

    ``from pkg import name`` yields ``pkg.name``: *name* may be a submodule
    (``from google.api_core import exceptions``), and where it is not, the
    resolver falls back to ``pkg``.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.extend((f"{node.module}.{alias.name}", node.lineno) for alias in node.names)
    return found


@cache
def _third_party_imports(directory: str) -> tuple[tuple[str, frozenset[str], str], ...]:
    """``(module, owning distributions, "path:line")`` per third-party import under *directory*."""
    found = []
    for path in sorted((_REPO_ROOT / directory).rglob("*.py")):
        for module, line in _imported_modules(path):
            top = module.partition(".")[0]
            if top in sys.stdlib_module_names or top in _FIRST_PARTY:
                continue
            site = f"{path.relative_to(_REPO_ROOT)}:{line}"
            found.append((module, _owning_distributions(module), site))
    return tuple(found)


def _unconditional_requirements(dist: str) -> frozenset[str]:
    """What *dist* requires with no extra requested, under this environment's markers."""
    requirements = (Requirement(spec) for spec in metadata.requires(dist) or ())
    return frozenset(
        canonicalize_name(req.name)
        for req in requirements
        if req.marker is None or req.marker.evaluate({"extra": ""})
    )


_SWEPT_DIRECTORIES = [directory for directories, _ in _SCOPES.values() for directory in directories]
_each_allowlist_entry = pytest.mark.parametrize(
    ("dist", "provider"),
    [(dist, provider) for dist, (provider, _) in _TRANSITIVE_BY_DESIGN.items()],
    ids=list(_TRANSITIVE_BY_DESIGN),
)


class TestImportsAreDeclared:
    @pytest.mark.parametrize("directory", _SWEPT_DIRECTORIES)
    def test_sweep_finds_imports(self, directory: str):
        """Guard the guard — a directory yielding no imports is vacuously clean.

        A renamed or moved directory is swept as empty rather than refused.
        """
        assert _third_party_imports(directory), f"swept no third-party imports under {directory}/"

    @pytest.mark.parametrize(
        ("source", "namespace", "owner"),
        [
            ("import google.api_core.exceptions", "google", "google-api-core"),
            ("from google.api_core import exceptions", "google", "google-api-core"),
            ("from google.cloud import storage", "google.cloud", "google-cloud-storage"),
        ],
        ids=["import", "from-package", "from-namespace"],
    )
    def test_namespace_import_resolves_to_its_own_distribution(
        self, tmp_path: Path, source: str, namespace: str, owner: str
    ):
        """Guard the guard — a namespace is shared; what is imported from it is not (#294).

        Resolved by top-level name, ``google`` answers with every distribution
        in the namespace, so one declared among them would pass all the rest.
        ``from google.cloud import storage`` must resolve ``storage``, not the
        namespace it is imported from.
        """
        assert len(_owning_distributions(namespace)) > 1, f"{namespace} is no longer shared"
        probe = tmp_path / "probe.py"
        probe.write_text(f"{source}\n")
        [(module, _)] = _imported_modules(probe)
        assert _owning_distributions(module) == {owner}

    @pytest.mark.parametrize("scope", list(_SCOPES))
    def test_every_import_is_declared(self, scope: str):
        """An import's version is bounded by this manifest, not by whatever pulls it in."""
        directories, allowed = _SCOPES[scope]
        undeclared: dict[str, list[str]] = defaultdict(list)
        for directory in directories:
            for module, owners, site in _third_party_imports(directory):
                if owners & (allowed | _TRANSITIVE_BY_DESIGN.keys()):
                    continue
                key = ", ".join(sorted(owners)) or f"{module} (no installed distribution)"
                if site not in undeclared[key]:  # `from pkg import a, b` is one site
                    undeclared[key].append(site)
        report = "\n".join(
            f"  {dist}: {sites[0]}" + (f" (+{len(sites) - 1} more)" if len(sites) > 1 else "")
            for dist, sites in sorted(undeclared.items())
        )
        assert not undeclared, (
            f"Imported under {', '.join(directories)} but not declared:\n{report}\n"
            "Declare each, capped at its next major — or, if it must stay transitive, "
            "add it to _TRANSITIVE_BY_DESIGN with the reason (#294)."
        )


class TestTransitiveByDesign:
    @_each_allowlist_entry
    def test_provider_still_requires_it(self, dist: str, provider: str):
        """The reason holds only while *provider* is where the module comes from.

        An ``ImportError`` would announce the provider dropping it — unless
        something else still installs it, which only this catches.
        """
        assert dist in _unconditional_requirements(provider), (
            f"{provider} no longer requires {dist}, so the reason it is left undeclared "
            f"no longer holds: {_TRANSITIVE_BY_DESIGN[dist][1]}"
        )

    @_each_allowlist_entry
    def test_it_stays_undeclared(self, dist: str, provider: str):
        """Declaring an entry keeps it installed after *provider* stops using it."""
        assert dist not in _WITH_DEV, (
            f"{dist} is declared in pyproject.toml, but must arrive only through "
            f"{provider}: {_TRANSITIVE_BY_DESIGN[dist][1]}"
        )

    @_each_allowlist_entry
    def test_it_is_still_imported(self, dist: str, provider: str):
        """An entry nothing imports is stale — drop it."""
        imported = {
            owner
            for directory in _SWEPT_DIRECTORIES
            for _, owners, _ in _third_party_imports(directory)
            for owner in owners
        }
        assert dist in imported, f"nothing imports {dist} any more; drop its allowlist entry"
