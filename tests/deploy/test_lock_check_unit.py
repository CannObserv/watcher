"""The unit's start step: the venv synced to uv.lock, offline, fail closed (#358).

``ExecStart`` is ``uv run uvicorn …``, and ``uv run`` syncs first. Since #358
co-core resolves from the co-pypi index, and a sync that has to download from
it fails without a token, which the unit never holds. So one ``ExecStartPre``
stands in front of it:

    uv sync --locked --offline

* **``--offline``**: no network call, so a boot that starts before the network
  can't trip it (#393's property, kept). Anything it would have to download
  fails it, exit 1, naming the file: a co-core file neither installed nor in
  uv's cache. Measured under ``unshare -n``.
* **``--locked``**: a ``pyproject.toml`` the lock no longer matches fails too.
* **It syncs, it does not ``--check``.** It rebuilds the project itself and
  removes extraneous packages, offline, and reinstalls a cached locked file
  against the lock's sha256. ``--check`` failed every start after *any*
  ``pyproject.toml`` edit, a comment included, until a manual sync: uv keys
  the project's own install on that file (measured on the #358 merge).
* **No ``-`` prefix**: a miss stops the start, at this step rather than at
  uvicorn's first import. The fix is the deploy's ``uv sync --locked``
  through ``scripts/co-pypi.sh``.

It replaces #393's two wheelhouse steps, which this asserts are gone.
"""

from pathlib import Path

from tests.deploy.systemd_units import directive_values

UNIT = Path(__file__).resolve().parents[2] / "deploy" / "watcher.service"
CHECK = "/usr/local/bin/uv sync --locked --offline"


def _pre_steps() -> list[str]:
    return directive_values(UNIT.read_text(), "ExecStartPre")


def test_the_check_is_fail_closed_and_offline():
    found = [line for line in _pre_steps() if "sync" in line and "uv" in line]
    assert found == [CHECK], "one uv step: the offline sync, with no `-` prefix"


def test_the_step_syncs_rather_than_checks():
    """``--check`` cannot rebuild the project, so a ``pyproject.toml`` edit alone
    left every unplanned restart failing until the deploy's sync (#358)."""
    assert not any("--check" in line for line in _pre_steps())


def test_the_check_is_the_last_step_before_exec_start():
    """After the runtime dir and BUILD_ID stamp, so it is what gates ``uv run``."""
    assert _pre_steps()[-1] == CHECK


def test_exec_start_is_still_uv_run():
    """The check holds the venv ``uv run`` syncs; if ExecStart stopped being a
    ``uv run`` of this project, the check would guard nothing it runs."""
    [start] = directive_values(UNIT.read_text(), "ExecStart")
    assert start.startswith("/usr/local/bin/uv run uvicorn ")


def test_the_wheelhouse_steps_are_gone():
    text = UNIT.read_text()
    assert "sync_wheelhouse" not in text
    assert "check_wheelhouse" not in text
    assert "google-cloud-storage" not in text, "nothing at start reaches GCS"
