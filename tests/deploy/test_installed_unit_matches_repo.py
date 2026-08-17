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
