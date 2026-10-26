"""Hold each wheelhouse file ``uv.lock`` names to its committed sha256 (#393).

``uv`` installs ``co-core`` / ``co-core-aio`` from ``./.wheelhouse`` by file
name alone: a ``find-links`` path source locks no digest, and a hand-added one
is gone at the next ``uv lock`` (measured by CannObserv/broker#125 on uv
0.13.5). So ``pyproject.toml``'s ``[tool.watcher.wheelhouse-sha256]`` pins one
per locked file, and this script checks the files on disk against it:

    uv run --no-project python scripts/check_wheelhouse.py

The systemd unit runs it as the ``ExecStartPre`` after the wheelhouse sync and
before ``uv run`` starts the service, **fail closed**: no ``-`` prefix, so a
locked file that is missing or misses its pin stops the start. It makes no
network call and imports only the standard library (no ``--with`` to resolve),
so a boot that starts before the network cannot trip it. The sync before it is
the fail-open, online half: it refetches a file that misses its pin.

``scripts/sync_wheelhouse.py`` imports ``TABLE``, ``pins`` and ``sha256``
from here, so the two read the lock and the table one way.

Exit codes: ``0`` every locked file matches its pin · ``1`` anything else
(a missing file, a missing or malformed pin, a mismatch, an unreadable lock
or one that names no wheelhouse file).
Output is plain text, one line, like the sync's (#247).
"""

from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
# pyproject.toml's table of digests, under [tool.watcher]: one per locked file.
TABLE = "wheelhouse-sha256"
# How uv.lock names the wheelhouse as a source: `.wheelhouse` today, and
# find-links' own `./.wheelhouse` spelling is taken too.
WHEELHOUSE_SOURCES = (".wheelhouse", "./.wheelhouse")
SHA256 = re.compile(r"[0-9a-f]{64}")


def locked_files(lock: dict) -> list[str]:
    """The wheelhouse files a parsed uv.lock names: the sdist and wheel paths of
    each package whose source is the wheelhouse."""
    names = []
    for package in lock.get("package", []):
        if package.get("source", {}).get("registry") not in WHEELHOUSE_SOURCES:
            continue
        for dist in [package.get("sdist", {}), *package.get("wheels", [])]:
            if "path" in dist:
                names.append(dist["path"])
    return names


def pins(project: Path) -> dict[str, str]:
    """``{file: sha256}`` for each wheelhouse file *project*'s uv.lock names;
    raises ``ValueError`` for a file with no pin, or a pin that is not a sha256."""
    lock = tomllib.loads((project / "uv.lock").read_text())
    pyproject = tomllib.loads((project / "pyproject.toml").read_text())
    table = pyproject.get("tool", {}).get("watcher", {}).get(TABLE, {})
    names = locked_files(lock)
    # Fail closed on nothing to hold: a lock that spells the source some new way
    # would otherwise pass the check with every row unchecked.
    if not names:
        raise ValueError(
            f"uv.lock names no wheelhouse file (a source of {' or '.join(WHEELHOUSE_SOURCES)})"
        )
    missing = [name for name in names if name not in table]
    if missing:
        raise ValueError(
            f"uv.lock names {', '.join(missing)}, which pyproject.toml's"
            f" [tool.watcher.{TABLE}] pins no digest for: pin each from"
            " `sha256sum .wheelhouse/<file>` (docs/DEPENDENCIES.md)"
        )
    # A blank pin must not read as no pin, and so as nothing to hold.
    malformed = [name for name in names if not SHA256.fullmatch(str(table[name]))]
    if malformed:
        raise ValueError(
            f"pyproject.toml's [tool.watcher.{TABLE}] pins {', '.join(malformed)}"
            " with a value that is not a sha256 (64 lowercase hex)"
        )
    return {name: table[name] for name in names}


def sha256(path: str | Path) -> str:
    """The hex sha256 of the file at *path*."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def check() -> int:
    """Hold ``PROJECT``'s locked wheelhouse files to their pins; return an exit code."""
    wheelhouse = PROJECT / ".wheelhouse"
    try:
        pinned = pins(PROJECT)
        failures = []
        for name, digest in pinned.items():
            path = wheelhouse / name
            if not path.is_file():
                failures.append(f"{name} is missing")
            elif (got := sha256(path)) != digest:
                failures.append(f"{name} has sha256 {got}, not the pinned {digest}")
        if failures:
            raise ValueError(f"{'; '.join(failures)} (pyproject.toml's [tool.watcher.{TABLE}])")
    except Exception as exc:  # broad by design: every failure here fails the start
        print(f"error: wheelhouse check failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"wheelhouse pins hold: {', '.join(pinned)} match"
        f" pyproject.toml's [tool.watcher.{TABLE}] -> {wheelhouse}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(check())
