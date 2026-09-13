"""The nightly backup's systemd units (#296 D8/D9, #297).

Pins what the unit files promise — the identity, the sandbox, the credential
boundary, the schedule — and, on a host that has them installed, that the
installed copies match (the ``test_installed_unit_matches_repo`` rule, for these
two files).

The shape was established on the VM, not reasoned (#297). The job runs as its
own dynamic user with no capabilities at all, so nothing it runs is root and
nothing drops privileges: ``pg_dump`` connects as that user, by peer auth, to
the ``pg_read_all_data`` role of the same name. The keys arrive as systemd
credentials, which never enter a process environment. Two systemd 255 facts
constrain it: a ``LoadCredential=`` source that is missing fails the start
(243/CREDENTIALS), and an empty ``SetCredential=`` fallback is ignored — so
both key files must exist, and the check-in key is an empty file until the
monitor does.
"""

import stat
from pathlib import Path

import pytest

from src.ops.checkin import KEY_CREDENTIAL

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "deploy" / "watcher-backup.service"
TIMER = REPO / "deploy" / "watcher-backup.timer"
ROLE_SCRIPT = REPO / "scripts" / "setup-backup-role.sql"
INSTALLED = Path("/etc/systemd/system")

CHECKOUT = "/home/exedev/watcher"
GCS_KEY = "/etc/watcher/co-watcher-backup.json"
CHECKIN_KEY = "/etc/watcher/backup-notifier.key"


def _values(text: str, directive: str) -> list[str]:
    prefix = f"{directive}="
    return [
        line.strip().removeprefix(prefix)
        for line in text.splitlines()
        if line.strip().startswith(prefix)
    ]


def _tokens(text: str, directive: str) -> set[str]:
    return {token for value in _values(text, directive) for token in value.split()}


class TestServiceIdentity:
    def test_runs_as_its_own_dynamic_user_never_root(self) -> None:
        """Allocated by systemd for each run: nothing to provision, no account
        to log in to, and a uid no other process holds."""
        text = SERVICE.read_text()
        assert _values(text, "User") == ["watcher_backup"]
        assert _values(text, "DynamicUser") == ["yes"]

    def test_holds_no_capabilities_at_all(self) -> None:
        """An empty bounding set, and nothing ambient. The three root needed —
        DAC_READ_SEARCH for the venv, SETUID/SETGID for setpriv — went with it."""
        text = SERVICE.read_text()
        assert _values(text, "CapabilityBoundingSet") == [""]
        assert _values(text, "AmbientCapabilities") == []

    def test_the_database_role_is_the_unit_user(self) -> None:
        """Peer auth maps the OS user to the role of the same name, and there is
        no ident map on this cluster to translate between two."""
        (user,) = _values(SERVICE.read_text(), "User")
        assert f"\\set backup_role {user}" in ROLE_SCRIPT.read_text()


class TestServiceSandbox:
    def test_confined(self) -> None:
        text = SERVICE.read_text()
        for directive, value in {
            "ProtectSystem": "strict",
            "ProtectHome": "tmpfs",
            "PrivateTmp": "yes",
            "NoNewPrivileges": "yes",
            "LockPersonality": "yes",
            "RestrictNamespaces": "yes",
            "ProtectKernelTunables": "yes",
            "ProtectKernelModules": "yes",
            "ProtectControlGroups": "yes",
        }.items():
            assert _values(text, directive) == [value], directive

    def test_sees_only_the_checkout_of_the_home(self) -> None:
        """An empty /home with the checkout bound back read-only — no ~/.ssh,
        no other checkout — and nothing bound writable."""
        text = SERVICE.read_text()
        assert _values(text, "WorkingDirectory") == [CHECKOUT]
        assert _values(text, "BindReadOnlyPaths") == [CHECKOUT]
        assert _values(text, "BindPaths") == []

    def test_reaches_only_the_socket_and_the_network(self) -> None:
        assert _tokens(SERVICE.read_text(), "RestrictAddressFamilies") == {
            "AF_UNIX",
            "AF_INET",
            "AF_INET6",
        }

    def test_dumps_as_itself_with_the_venv_interpreter(self) -> None:
        """The venv's python directly, not ``uv run``: uv wants a writable cache
        and may sync the environment, both refused by the sandbox (broker#4).
        No ``--run-as``: the job is already the user the database knows."""
        (exec_start,) = _values(SERVICE.read_text(), "ExecStart")
        assert exec_start.startswith(f"{CHECKOUT}/.venv/bin/python -m src.ops.backup")
        assert "--database watcher" in exec_start
        assert "--run-as" not in exec_start

    def test_the_interpreter_is_visible_inside_the_sandbox(self) -> None:
        """The venv's ``python`` is a link to the base interpreter. Here that is
        /usr/bin/python3.12; a uv-managed one would sit under ~/.local/share/uv,
        hidden by ProtectHome=tmpfs, and the unit would fail to exec (203/EXEC)
        on the one host where nobody runs it by hand before the timer does."""
        interpreter = Path(_values(SERVICE.read_text(), "ExecStart")[0].split()[0])
        if not interpreter.exists():
            pytest.skip(f"{interpreter} not present — not a host running the backup")
        resolved = interpreter.resolve()
        bound = [Path(p) for p in _values(SERVICE.read_text(), "BindReadOnlyPaths")]
        assert not resolved.is_relative_to("/home") or any(
            resolved.is_relative_to(path) for path in bound
        ), f"{interpreter} resolves to {resolved}, which the unit's empty /home hides"


class TestServiceCredentials:
    def test_keys_arrive_as_credentials_not_environment(self) -> None:
        """systemd reads each root-only file and hands the run a private copy
        under $CREDENTIALS_DIRECTORY. The GCS SDK takes a path, so it gets the
        copy's; the check-in reads its key from the directory itself — by the
        name ``src.ops.checkin`` reads, imported rather than spelled again, so
        renaming either side fails here instead of leaving the job keyless."""
        text = SERVICE.read_text()
        assert set(_values(text, "LoadCredential")) == {
            f"gcs:{GCS_KEY}",
            f"{KEY_CREDENTIAL}:{CHECKIN_KEY}",
        }
        assert "GOOGLE_APPLICATION_CREDENTIALS=%d/gcs" in _values(text, "Environment")

    def test_loads_its_own_configuration_and_nothing_of_the_services(self) -> None:
        """One file, required — no bucket is a failed start, not a quiet one.
        Not /etc/watcher/.env — it carries the database URLs and the bus URL,
        which a job that holds no database credential must not have — and never
        notifier.env, which is watcher.service's alone (#278)."""
        assert _values(SERVICE.read_text(), "EnvironmentFile") == ["/etc/watcher/backup.env"]

    @pytest.mark.parametrize("key", [GCS_KEY, CHECKIN_KEY], ids=["gcs", "notifier-key"])
    def test_an_installed_key_is_readable_only_by_root(self, key: str) -> None:
        """Only systemd reads the file; the job reads its private copy. Mode is
        read with ``stat``, never the contents."""
        path = Path(key)
        if not path.exists():
            pytest.skip(f"{path} not installed — not a host running the backup")
        info = path.stat()
        assert info.st_uid == 0, f"{path} must be owned by root"
        assert not info.st_mode & (stat.S_IRWXG | stat.S_IRWXO), (
            f"{path} is mode {stat.S_IMODE(info.st_mode):o}; install it 0400 root:root"
        )


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
