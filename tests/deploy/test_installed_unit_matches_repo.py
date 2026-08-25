"""The installed systemd unit must match the repo copy.

Ported from archiver (#233). ``deploy/watcher.service`` gained
``Environment=WATCHER_ALLOW_PRODUCTION_DB=1``, without which the service
refuses to start once ``src.core.db_safety`` is deployed. That unit is
installed to ``/etc/systemd/system/`` by hand, and nothing verified it stayed
in sync afterwards.

Which is the same failure class as the incident that started this workstream:
the deployed thing quietly diverging from the documented thing. A future edit
to ``deploy/watcher.service`` that never reaches the VM would either fail to
apply a needed setting or, worse, leave the service unable to start on its
next restart.

Skips when the unit is not installed, so CI and dev clones pass; it only
asserts on a host that actually runs the service.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_UNIT = Path(__file__).resolve().parents[2] / "deploy" / "watcher.service"
INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")


def _read_if_installed(path: Path) -> str | None:
    """Return the unit's text, or None when it is genuinely not installed.

    Only ``FileNotFoundError`` means "not installed". A ``PermissionError`` is
    deliberately allowed to propagate: swallowing it would turn an unreadable
    unit into a silent pass, and a drift check that cannot fail is worse than
    no check at all — it reads as coverage while asserting nothing.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _unset_environment(unit_text: str) -> set[str]:
    """Return every variable named by an ``UnsetEnvironment=`` line.

    ``UnsetEnvironment=`` takes a space-separated list and may be repeated, so
    the directive's meaning is the union of its tokens — not the text of any
    one line. Parsing keeps the assertions in this module indifferent to how a
    future edit chooses to group the names.
    """
    names: set[str] = set()
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("UnsetEnvironment="):
            names.update(stripped.split("=", 1)[1].split())
    return names


def test_repo_unit_declares_the_production_opt_in() -> None:
    """The opt-in must live in the unit, never in an env file.

    An EnvironmentFile is sourced by every process that loads it — putting the
    flag there would re-open the hole for hand-run servers, which is exactly
    what the guard exists to close.
    """
    text = REPO_UNIT.read_text()
    assert "Environment=WATCHER_ALLOW_PRODUCTION_DB=1" in text


def test_repo_unit_drops_the_migration_credential() -> None:
    """#270: the service process must not inherit the DDL credential.

    ``WATCHER_MIGRATION_DATABASE_URL`` lives in ``/etc/watcher/.env`` beside
    ``DATABASE_URL``, and the unit loads that file wholesale — so the service
    inherited the one credential that can drop tables even though the
    connection it opens is the DML-only ``watcher_app`` one (#259). Only
    ``alembic/env.py`` ever reads it; nothing in the running service does, and
    ``alembic`` run from a shell still resolves it from the env file.

    ``UnsetEnvironment=``, not ``Environment=WATCHER_MIGRATION_DATABASE_URL=``;
    the unit comment on that line explains why blanking cannot work.

    Asserted on the parsed token rather than the whole line, because
    ``UnsetEnvironment=`` takes a space-separated list: a second variable added
    later is valid systemd and must not read as a regression here.
    """
    assert "WATCHER_MIGRATION_DATABASE_URL" in _unset_environment(REPO_UNIT.read_text())


def test_repo_unit_declares_the_bus_opt_in() -> None:
    """#262: the bus gate is unit-only, for the same reason as the DB one.

    ``WATCHER_BUS_REDIS_URL`` lives in ``/etc/watcher/.env``, so every process
    that sources it inherits a broker address. The flag is what separates the
    service from an agent shell or a REPL, and it only works if no env file
    carries it. Without this line the service starts and refuses to publish —
    which is why ``src/core/bus.py`` also makes the URL-without-flag
    combination a startup failure rather than a silent one.
    """
    text = REPO_UNIT.read_text()
    assert "Environment=WATCHER_BUS_ENABLED=1" in text


def test_repo_unit_declares_the_notifier_opt_in() -> None:
    """#277: the notifier gate is unit-only, for the same reason as the other two.

    ``WATCHER_NOTIFIER_BASE_URL`` and ``WATCHER_NOTIFIER_API_KEY`` lived in
    ``/etc/watcher/.env`` when this flag was added, so every process that
    sourced it inherited the production tenant's credentials. The flag is what
    separated the service from a pytest run, a hand-run dev server, a script or
    a REPL — and it only works if no env file carries it. #278 moved the pair
    to ``/etc/watcher/notifier.env`` so nothing but the unit holds it either;
    the flag stays, because it is what the app checks
    (``tests/deploy/test_notifier_credential_is_unit_only.py`` owns the move).

    Without this line the service starts and refuses to notify,
    which is why ``src/core/notifier_client`` also makes the URL-without-flag
    combination a startup failure rather than a silent one.
    """
    text = REPO_UNIT.read_text()
    assert "Environment=WATCHER_NOTIFIER_ENABLED=1" in text


def test_repo_unit_treats_sigterm_exit_as_success() -> None:
    """#256: a graceful stop must not read as a failure.

    uvicorn exits 143 (128+15) on SIGTERM, which is the normal path for
    ``systemctl stop``. Without ``SuccessExitStatus=143`` systemd files that
    under ``failed``, so ``systemctl is-active watcher`` — the one signal an
    operator checks first — cannot tell a routine stop from a crash, and the
    journal has to be read to find out which happened.
    """
    text = REPO_UNIT.read_text()
    assert "SuccessExitStatus=143" in text


def test_systemd_has_loaded_the_installed_unit() -> None:
    """A copied unit that was never ``daemon-reload``ed is still not in force.

    ``test_installed_unit_matches_repo`` compares bytes on disk. systemd serves
    the unit it parsed at the last reload, so ``sudo cp`` alone leaves every
    other assertion in this module green while the running service keeps its
    old configuration.

    #270 is what made that gap worth closing: ``WATCHER_ALLOW_PRODUCTION_DB``
    and ``WATCHER_BUS_ENABLED`` announce a missed reload the next time the
    service restarts — it fails to start, or refuses to publish.
    ``UnsetEnvironment=`` announces nothing. The credential is simply still
    there, and the only signal is this one.

    Skips off-host for the same reason the drift check does; ``NeedDaemonReload``
    is readable without privileges, so nothing here needs root.
    """
    if _read_if_installed(INSTALLED_UNIT) is None:
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        pytest.skip("systemctl not available")

    result = subprocess.run(
        [systemctl, "show", "watcher", "--property=NeedDaemonReload", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"systemctl show failed: {result.stderr.strip()}")

    assert result.stdout.strip() == "no", (
        "systemd has not reloaded the installed unit — the file at "
        f"{INSTALLED_UNIT} is not what the service is running.\n"
        "Reload with:\n"
        "  sudo systemctl daemon-reload\n"
        "Then restart the service when it is safe to do so."
    )


def test_installed_unit_matches_repo() -> None:
    installed = _read_if_installed(INSTALLED_UNIT)
    if installed is None:
        pytest.skip(f"{INSTALLED_UNIT} not present — not a host running the service")
    assert installed == REPO_UNIT.read_text(), (
        f"{INSTALLED_UNIT} has drifted from {REPO_UNIT}.\n"
        "Reinstall with:\n"
        f"  sudo cp {REPO_UNIT} {INSTALLED_UNIT} && sudo systemctl daemon-reload\n"
        "Then restart the service when it is safe to do so."
    )
