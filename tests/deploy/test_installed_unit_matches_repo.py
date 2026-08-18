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
    ``alembic/env.py`` ever reads it; nothing in the running service does.

    ``UnsetEnvironment=``, not the ``Environment=WATCHER_MIGRATION_DATABASE_URL=``
    that #270 proposed. Blanking does not work at any position in the unit:
    systemd.exec is explicit that "settings from these files override settings
    made with ``Environment=``" — file over inline, not last-one-wins — so the
    env file simply reinstates the credential. ``UnsetEnvironment=`` is applied
    as the final step of environment compilation and undoes assignments from
    every source, which is the only directive that can remove a variable an
    ``EnvironmentFile=`` sets. Verified against systemd 255 on the host.

    Inert everywhere else: the variable stays in the env file, so ``alembic``
    run from a shell still resolves it.
    """
    text = REPO_UNIT.read_text()
    assert "UnsetEnvironment=WATCHER_MIGRATION_DATABASE_URL\n" in text


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
