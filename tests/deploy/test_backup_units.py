"""The nightly backup's systemd units (#296 D8/D9).

Pins what the unit files promise — the sandbox, the credential boundary, the
schedule — and, on a host that has them installed, that the installed copies
match (the ``test_installed_unit_matches_repo`` rule, for these two files).

The sandbox shape was established on the VM, not reasoned: under systemd 255 a
seccomp-backed protection (``LockPersonality``, ``RestrictAddressFamilies``,
``RestrictNamespaces``, ``ProtectKernelModules``, ``ProtectKernelTunables``)
beside ``NoNewPrivileges=yes`` strips ``CAP_SETUID`` from the service's
effective set before it execs, and ``setpriv`` then fails ``setresuid`` with
EPERM. ``AmbientCapabilities=CAP_SETUID CAP_SETGID`` keeps it through the exec,
and the dropped-to ``postgres`` child still holds no capabilities at all.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "deploy" / "watcher-backup.service"
TIMER = REPO / "deploy" / "watcher-backup.timer"
INSTALLED = Path("/etc/systemd/system")


def _values(text: str, directive: str) -> list[str]:
    prefix = f"{directive}="
    return [
        line.strip().removeprefix(prefix)
        for line in text.splitlines()
        if line.strip().startswith(prefix)
    ]


def _tokens(text: str, directive: str) -> set[str]:
    return {token for value in _values(text, directive) for token in value.split()}


class TestServiceSandbox:
    def test_runs_as_root_confined(self) -> None:
        """Root to read its 0400 key; everything else is taken away."""
        text = SERVICE.read_text()
        assert _values(text, "User") == ["root"]
        for directive, value in {
            "ProtectSystem": "strict",
            "ProtectHome": "read-only",
            "PrivateTmp": "yes",
            "NoNewPrivileges": "yes",
            "LockPersonality": "yes",
            "RestrictNamespaces": "yes",
            "ProtectKernelTunables": "yes",
            "ProtectKernelModules": "yes",
            "ProtectControlGroups": "yes",
        }.items():
            assert _values(text, directive) == [value], directive

    def test_holds_exactly_the_capabilities_it_uses(self) -> None:
        """DAC_READ_SEARCH reads the venv under the 0750 home; SETUID/SETGID let
        setpriv drop to postgres. Ambient, for the systemd 255 reason above."""
        text = SERVICE.read_text()
        assert _tokens(text, "CapabilityBoundingSet") == {
            "CAP_DAC_READ_SEARCH",
            "CAP_SETUID",
            "CAP_SETGID",
        }
        assert _tokens(text, "AmbientCapabilities") == {"CAP_SETUID", "CAP_SETGID"}

    def test_reaches_only_the_socket_and_the_network(self) -> None:
        assert _tokens(SERVICE.read_text(), "RestrictAddressFamilies") == {
            "AF_UNIX",
            "AF_INET",
            "AF_INET6",
        }

    def test_dumps_as_postgres_with_the_venv_interpreter(self) -> None:
        """The venv's python directly, not ``uv run``: uv wants a writable cache
        and may sync the environment, both refused by the sandbox (broker#4)."""
        (exec_start,) = _values(SERVICE.read_text(), "ExecStart")
        assert exec_start.startswith("/home/exedev/watcher/.venv/bin/python -m src.ops.backup")
        assert "--run-as postgres" in exec_start
        assert "--database watcher" in exec_start


class TestServiceCredentials:
    def test_loads_its_own_configuration_and_nothing_of_the_services(self) -> None:
        """Not /etc/watcher/.env — it carries the database URLs and the bus
        URL, which a job that holds no database credential must not have — and
        never notifier.env, which is watcher.service's alone (#278)."""
        files = [f.removeprefix("-") for f in _values(SERVICE.read_text(), "EnvironmentFile")]
        assert "/etc/watcher/backup.env" in files
        assert "/etc/watcher/.env" not in files
        assert "/etc/watcher/notifier.env" not in files
        assert not any(f.startswith("/home/") for f in files)

    def test_its_configuration_is_required_and_its_check_in_is_not(self) -> None:
        """No bucket is a failed start, not a quiet one. The check-in credential
        is optional only until the monitor exists: the job warns without it."""
        text = SERVICE.read_text()
        assert "EnvironmentFile=/etc/watcher/backup.env" in text
        assert "EnvironmentFile=-/etc/watcher/backup-notifier.env" in text


class TestTimer:
    def test_nightly_and_catches_up_after_downtime(self) -> None:
        text = TIMER.read_text()
        (calendar,) = _values(text, "OnCalendar")
        assert calendar.endswith("UTC")
        assert _values(text, "Persistent") == ["true"]
        assert _values(text, "Unit") == ["watcher-backup.service"]


@pytest.mark.parametrize("unit", [SERVICE, TIMER], ids=lambda p: p.name)
def test_installed_copy_matches_repo(unit: Path) -> None:
    installed = INSTALLED / unit.name
    try:
        text = installed.read_text()
    except FileNotFoundError:
        pytest.skip(f"{installed} not installed — not a host running the backup")
    assert text == unit.read_text(), (
        f"{installed} has drifted from {unit}.\n"
        f"Reinstall with:\n  sudo cp {unit} {installed} && sudo systemctl daemon-reload"
    )
