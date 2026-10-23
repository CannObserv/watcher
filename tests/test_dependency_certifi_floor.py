"""Regression guard: the installed certifi no longer trusts the eMudhra chain (#391).

The keys of eight eMudhra TLS intermediates leaked; they chain to **emSign ECC
Root CA - G3** (GHSA-6q48-6c7w-9gw2, PYSEC-2026-4320). httpx and requests trust
certifi's bundle *instead of* the OS store, so the OS-level remediation does not
reach them: the GCS calls (backup, restore, blobs) and the operator probe kept
accepting the compromised chain on certifi 2026.2.25.

certifi is transitive — nothing here imports it — so the floor is a
``[tool.uv] constraint-dependencies`` entry, not a dependency line. These tests
pin three things: the floor is declared, the *installed* version meets it (a
re-lock cannot slip below), and the bundle itself lacks G3. certifi 2026.10.19
deliberately keeps emSign G1, C1 and C3, so the check is for G3, not for "no
emSign".

**Precondition:** the environment is synced from ``uv.lock``, as for
``tests/test_dependency_extras.py``.
"""

import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
_FLOOR = Version("2026.10.19")
_DISTRUSTED_ROOT = "CN=emSign ECC Root CA - G3"


def _certifi_constraints() -> list[Requirement]:
    """The ``certifi`` entries of ``[tool.uv] constraint-dependencies``."""
    manifest = tomllib.loads(_PYPROJECT.read_text())
    entries = manifest.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    return [req for req in map(Requirement, entries) if req.name == "certifi"]


def test_manifest_floors_certifi() -> None:
    """``constraint-dependencies`` excludes every certifi below the floor."""
    constraints = _certifi_constraints()
    assert constraints, "pyproject.toml declares no certifi constraint-dependency"
    for req in constraints:
        # The release #391 measured trusting G3, and the boundary below the floor.
        for vulnerable in ("2026.2.25", "2026.10.18"):
            assert not req.specifier.contains(vulnerable, prereleases=True), (
                f"{req} admits certifi {vulnerable}, below {_FLOOR}"
            )
        assert req.specifier.contains(str(_FLOOR)), f"{req} excludes the fixed release"


def test_installed_certifi_meets_floor() -> None:
    """The certifi in this environment is the fixed release or later."""
    installed = Version(metadata.version("certifi"))
    assert installed >= _FLOOR, f"certifi {installed} < {_FLOOR}: still trusts emSign G3"


def test_bundle_omits_emsign_g3() -> None:
    """The bundle httpx and requests load no longer carries the compromised root."""
    # certifi.where() without importing an undeclared module: the bundle is
    # certifi/cacert.pem in the installed distribution.
    bundle = Path(metadata.distribution("certifi").locate_file("certifi/cacert.pem")).read_text()
    assert _DISTRUSTED_ROOT not in bundle
