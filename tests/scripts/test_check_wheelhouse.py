"""scripts/check_wheelhouse.py, the offline digest check before ``uv run`` (#393).

A wheelhouse file ``uv.lock`` names is what ``uv`` installs, and uv records no
digest for one: a find-links path locks by file name alone. So pyproject.toml's
``[tool.watcher.wheelhouse-sha256]`` carries one per file, and the unit holds
each to it before ``ExecStart``, fail closed. The check makes no network call,
so a boot race cannot trip it; the GCS sync before it stays fail-open.

The real pin is held here too: every wheelhouse file the lock names has a
digest and no digest outlives its file, so the next ``co-core`` bump cannot
re-lock without moving the table in the same diff.
"""

import ast
import hashlib
import re
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import check_wheelhouse

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_wheelhouse.py"
TABLE = "wheelhouse-sha256"

WHEEL = "co_core-9.9.0-py3-none-any.whl"
SDIST = "co_core-9.9.0.tar.gz"
OTHER = "other-1.0-py3-none-any.whl"
GOOD = {WHEEL: b"the wheel", SDIST: b"the sdist", OTHER: b"other"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lock(*, source: str = ".wheelhouse") -> str:
    """A uv.lock with one wheelhouse package (wheel + sdist) and one from PyPI."""
    return f"""version = 1

[[package]]
name = "co-core"
version = "9.9.0"
source = {{ registry = "{source}" }}
sdist = {{ path = "{SDIST}" }}
wheels = [
    {{ path = "{WHEEL}" }},
]

[[package]]
name = "pydantic"
version = "2.12.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://files.example/pydantic-2.12.0.tar.gz", hash = "sha256:00" }}
"""


def pyproject(digests: dict[str, str]) -> str:
    rows = "".join(f'"{name}" = "{value}"\n' for name, value in digests.items())
    return f'[project]\nname = "t"\n\n[tool.watcher.{TABLE}]\n{rows}'


class Checkout:
    """A checkout's root: its lock, its pins, and its wheelhouse."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.wheelhouse = root / ".wheelhouse"
        self.wheelhouse.mkdir()
        (root / "uv.lock").write_text(lock())
        self.pin({WHEEL: sha(GOOD[WHEEL]), SDIST: sha(GOOD[SDIST])})
        for name in (WHEEL, SDIST):
            self.plant(name, GOOD[name])

    def pin(self, digests: dict[str, str]) -> None:
        (self.root / "pyproject.toml").write_text(pyproject(digests))

    def plant(self, name: str, data: bytes) -> None:
        (self.wheelhouse / name).write_bytes(data)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(check_wheelhouse, "PROJECT", tmp_path)
    return Checkout(tmp_path)


def run(capsys) -> tuple[int, str, str]:
    rc = check_wheelhouse.check()
    out = capsys.readouterr()
    return rc, out.out, out.err


class TestTheCheck:
    def test_files_that_match_their_pins_pass_and_are_named(self, checkout, capsys):
        """The journal line the deploy's verify reads: which files, held to what."""
        rc, out, err = run(capsys)
        assert rc == 0, err
        assert WHEEL in out and SDIST in out and TABLE in out
        assert not err

    def test_a_same_size_wheel_with_other_bytes_fails(self, checkout, capsys):
        """The swap a size-only skip kept forever: same name, same size."""
        planted = b"the WHEEL"
        assert len(planted) == len(GOOD[WHEEL])
        checkout.plant(WHEEL, planted)
        rc, out, err = run(capsys)
        assert rc == 1
        assert WHEEL in err and sha(planted) in err and sha(GOOD[WHEEL]) in err
        assert not out

    def test_the_sdist_is_held_to_its_pin_as_well(self, checkout, capsys):
        checkout.plant(SDIST, b"the SDIST")
        rc, _, err = run(capsys)
        assert rc == 1
        assert SDIST in err

    def test_every_failing_file_is_named_not_only_the_first(self, checkout, capsys):
        checkout.plant(WHEEL, b"the WHEEL")
        checkout.plant(SDIST, b"the SDIST")
        rc, _, err = run(capsys)
        assert rc == 1
        assert WHEEL in err and SDIST in err

    def test_a_missing_locked_file_fails(self, checkout, capsys):
        (checkout.wheelhouse / WHEEL).unlink()
        rc, _, err = run(capsys)
        assert rc == 1
        assert WHEEL in err and "missing" in err

    def test_a_locked_file_with_no_digest_fails_and_says_how_to_pin_it(self, checkout, capsys):
        checkout.pin({WHEEL: sha(GOOD[WHEEL])})
        rc, _, err = run(capsys)
        assert rc == 1
        assert SDIST in err and TABLE in err
        assert "sha256sum .wheelhouse/" in err

    @pytest.mark.parametrize("value", ["", "abc123", "A" * 64])
    def test_a_pin_that_is_not_a_sha256_fails(self, checkout, capsys, value):
        """A blank pin must not read as no pin, and so as nothing to hold."""
        checkout.pin({WHEEL: value, SDIST: sha(GOOD[SDIST])})
        rc, _, err = run(capsys)
        assert rc == 1
        assert WHEEL in err and "not a sha256" in err

    def test_a_file_the_lock_does_not_name_is_not_checked(self, checkout, capsys):
        """The wheelhouse mirrors every cannobserv release; only the locked ones install."""
        checkout.plant(OTHER, b"anything")
        rc, out, err = run(capsys)
        assert rc == 0, err
        assert OTHER not in out

    def test_a_lock_that_spells_the_wheelhouse_as_pyproject_does_is_still_read(
        self, checkout, capsys
    ):
        """``./.wheelhouse`` is find-links' spelling; a lock may carry either."""
        (checkout.root / "uv.lock").write_text(lock(source="./.wheelhouse"))
        checkout.plant(WHEEL, b"the WHEEL")
        rc, _, err = run(capsys)
        assert rc == 1
        assert WHEEL in err

    def test_a_lock_that_names_no_wheelhouse_file_fails_closed(self, checkout, capsys):
        """A lock spelling the source some new way reads as nothing to hold:
        that must stop the start, not pass it with every row unchecked."""
        (checkout.root / "uv.lock").write_text(lock(source="/abs/.wheelhouse"))
        rc, out, err = run(capsys)
        assert rc == 1
        assert "no wheelhouse file" in err
        assert not out

    def test_an_unreadable_lock_fails_closed(self, checkout, capsys):
        (checkout.root / "uv.lock").unlink()
        rc, _, err = run(capsys)
        assert rc == 1
        assert err.startswith("error: ")


class TestTheScriptIsOffline:
    def test_it_imports_only_the_standard_library(self):
        """The unit runs it as ``uv run --no-project`` with no ``--with``: a
        third-party import would need a resolve, and a resolve can need the
        index, which is the boot race the check exists to stay out of."""
        tree = ast.parse(SCRIPT.read_text())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules.add(node.module.split(".")[0])
        assert modules, "swept no imports"
        assert modules <= sys.stdlib_module_names | {"__future__"}, modules


def real_locked_files() -> set[str]:
    data = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    return set(check_wheelhouse.locked_files(data))


def real_pins() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["tool"]["watcher"][TABLE]


class TestTheRealPin:
    def test_the_lock_names_co_core_and_co_core_aio_from_the_wheelhouse(self):
        names = real_locked_files()
        assert any(name.startswith("co_core-") for name in names)
        assert any(name.startswith("co_core_aio-") for name in names)

    def test_every_locked_wheelhouse_file_has_a_digest_and_no_digest_outlives_one(self):
        """A re-lock onto a new co-core names files the table lacks: red here
        until the bump pins them (docs/DEPENDENCIES.md → the upgrade)."""
        assert set(real_pins()) == real_locked_files()

    def test_each_digest_is_a_sha256(self):
        for name, value in real_pins().items():
            assert re.fullmatch(r"[0-9a-f]{64}", value), name

    def test_the_synced_files_are_the_pinned_ones(self):
        """Where the wheelhouse is synced: the host, and CI after its sync step."""
        wheelhouse = REPO_ROOT / ".wheelhouse"
        present = {n: wheelhouse / n for n in real_pins() if (wheelhouse / n).is_file()}
        if not present:
            pytest.skip("no synced wheelhouse here")
        for name, path in present.items():
            assert sha(path.read_bytes()) == real_pins()[name], name
