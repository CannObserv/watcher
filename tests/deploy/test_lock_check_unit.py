"""The unit's start check: the venv matches uv.lock, offline, fail closed (#358).

``ExecStart`` is ``uv run uvicorn …``, and ``uv run`` syncs first. Since #358
co-core resolves from the co-pypi index, and a sync that has to download from
it fails without a token, which the unit never holds. So one ``ExecStartPre``
stands in front of it:

    uv sync --locked --offline --check

* **``--check``**: it changes nothing, and exits 1 when the venv differs from
  the lock, a locked package missing or an extraneous one present.
* **``--locked``**: a ``pyproject.toml`` the lock no longer matches fails too.
* **``--offline``**: no network call, so a boot that starts before the network
  can't trip it (#393's property, kept). Measured under ``unshare -n``.
* **No ``-`` prefix**: a miss stops the start, at the check rather than at
  uvicorn's first import. The fix is the deploy's ``uv sync --locked``
  through ``scripts/co-pypi.sh``.

It replaces #393's two wheelhouse steps, which this asserts are gone.
"""

from pathlib import Path

from tests.deploy.systemd_units import directive_values

UNIT = Path(__file__).resolve().parents[2] / "deploy" / "watcher.service"
CHECK = "/usr/local/bin/uv sync --locked --offline --check"


def _pre_steps() -> list[str]:
    return directive_values(UNIT.read_text(), "ExecStartPre")


def test_the_check_is_fail_closed_and_offline():
    found = [line for line in _pre_steps() if "sync" in line and "uv" in line]
    assert found == [CHECK], "one uv step: the check, with no `-` prefix"


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
