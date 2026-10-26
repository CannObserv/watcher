"""The unit's wheelhouse steps: sync fail-open, then check fail-closed (#393).

``ExecStart``'s ``uv run`` installs co-core from ``.wheelhouse`` by file name
alone. Two ``ExecStartPre`` steps stand in front of it:

1. ``scripts/sync_wheelhouse.py`` (``-`` prefix): GCS is reached over the
   network, which a boot can start before; a failure there is journaled and the
   start goes on, as it has since #220. With the network up it also repairs a
   locked file that misses its pin.
2. ``scripts/check_wheelhouse.py`` (no prefix): offline, so no boot race trips
   it; a locked file missing or missing its pin stops the start. Second, so it
   hashes the bytes ``uv run`` is about to install, after any repair.
"""

from pathlib import Path

from tests.deploy.systemd_units import directive_values

UNIT = Path(__file__).resolve().parents[2] / "deploy" / "watcher.service"
CHECK = "/home/exedev/watcher/scripts/check_wheelhouse.py"


def _pre_steps() -> list[str]:
    return directive_values(UNIT.read_text(), "ExecStartPre")


def _step(script: str) -> tuple[int, str]:
    found = [(i, line) for i, line in enumerate(_pre_steps()) if script in line]
    assert len(found) == 1, f"want one ExecStartPre running {script}, found {found}"
    return found[0]


def test_the_sync_stays_fail_open():
    _, line = _step("scripts/sync_wheelhouse.py")
    assert line.startswith("-/usr/local/bin/uv run --no-project ")


def test_the_check_is_fail_closed_and_offline():
    _, line = _step("scripts/check_wheelhouse.py")
    # No `-`: a failure stops the start. No `--with`: nothing to resolve.
    assert line == f"/usr/local/bin/uv run --no-project python {CHECK}"


def test_the_check_runs_after_the_sync():
    sync_at, _ = _step("scripts/sync_wheelhouse.py")
    check_at, _ = _step("scripts/check_wheelhouse.py")
    assert check_at > sync_at
