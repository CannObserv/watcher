"""Regression guard: the runtime venv holds each advisory's fixed release (#392).

``uv audit --frozen --no-dev`` on ``223eac7`` reported 20 unique advisories in
six runtime packages. Each was fixed by moving the lock alone
(``uv lock --upgrade-package <pkg>``): python-multipart is the only direct
dependency among them, and its declared floor (``>=0.0.22``) stays where it is,
as do fastapi's — fastapi 0.135.1 already admits starlette 1.x.

So the floor lives in ``uv.lock``, and this test is what keeps it: it fails if
the *installed* version of any row drops below the release that fixed its
advisories — a hand-edited lock, a resolver backtrack, a downgrade to dodge a
breaking change. It deliberately does not query OSV: that is ``uv audit``'s job,
needs the network, and would redden CI on the day an advisory is published
rather than on the commit that regressed.

Add a row when an advisory is fixed by a lock bump; drop one only with the
package. **Precondition:** the environment is synced from ``uv.lock``, as for
``tests/test_dependency_extras.py``.
"""

from importlib import metadata

import pytest
from packaging.version import Version

# package -> (first release fixing every advisory listed, advisories)
_FLOORS: dict[str, tuple[str, str]] = {
    "python-multipart": ("0.0.31", "PYSEC-2026-3036, -3037, -3038, -3039, -3040, -3041"),
    "urllib3": ("2.8.0", "PYSEC-2026-4175, -4176, -4177"),
    "idna": ("3.15", "PYSEC-2026-215"),
    "mako": ("1.4.2", "PYSEC-2026-88, -2617, -4243"),
    "starlette": ("1.3.1", "PYSEC-2026-161, -248, -249, -2280, -2281"),
    "anyio": ("4.14.2", "PYSEC-2026-4024, -4025"),
}


@pytest.mark.parametrize("package", sorted(_FLOORS))
def test_installed_meets_advisory_floor(package: str) -> None:
    """The installed release is the one that fixed the row's advisories, or later."""
    floor, advisories = _FLOORS[package]
    installed = Version(metadata.version(package))
    assert installed >= Version(floor), (
        f"{package} {installed} < {floor}: still exposed to {advisories} (#392)"
    )
