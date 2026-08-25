"""The production notifier credential must not live in the shared env file (#278).

#277 gave the notifier a unit-only *permission* flag: every process that
sources ``/etc/watcher/.env`` still held ``WATCHER_NOTIFIER_BASE_URL`` and
``WATCHER_NOTIFIER_API_KEY``, but only ``deploy/watcher.service`` set
``WATCHER_NOTIFIER_ENABLED=1``, so only the service could build a client.

That closed the *use*, not the *holding*. Notifier's audit (CannObserv/notifier#22,
watcher#278) found ~1289 watcher fixture notifications delivered to the real
Slack and Mailgun channels on the real production key, and named the shared env
file as the bug: AGENTS.md tells every agent to ``source scripts/load-env.sh``,
which exports that file, so every agent shell, script and REPL on this VM was
handed the production tenant's credential. A flag stops the accident; it does
not stop a deliberate ``export WATCHER_NOTIFIER_ENABLED=1``, and it does nothing
about the credential sitting in a shell's environment where anything can read it.

So the credential moves to ``/etc/watcher/notifier.env``, which **only the
systemd unit loads** — the same separation ``Environment=`` already gives the
three opt-in flags, applied to the secret itself. ``scripts/load-env.sh`` does
not know the path, so no shell inherits it.

The on-host assertions skip on any machine that does not run the service, the
same way ``test_installed_unit_matches_repo`` does.
"""

import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_UNIT = REPO_ROOT / "deploy" / "watcher.service"
LOAD_ENV = REPO_ROOT / "scripts" / "load-env.sh"

SHARED_ENV_FILE = Path("/etc/watcher/.env")
NOTIFIER_ENV_FILE = Path("/etc/watcher/notifier.env")

#: The two variables that together make a dispatch deliverable.
NOTIFIER_CREDENTIAL_VARS = ("WATCHER_NOTIFIER_BASE_URL", "WATCHER_NOTIFIER_API_KEY")


def _defined_keys(path: Path) -> set[str]:
    """Return the variable names an env file assigns.

    Deliberately name-only: this module must never put a secret's *value* into
    a test report or a CI log. Parsing mirrors ``load_env_file`` in
    ``scripts/load-env.sh`` — skip blanks and comments, tolerate ``export K=v``,
    take everything left of the first ``=``.
    """
    keys: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_repo_unit_loads_the_notifier_env_file() -> None:
    """The unit is the only loader of the credential.

    Required, not optional (no ``-`` prefix): a missing file must fail the
    start. With ``-`` the service would come up holding
    ``WATCHER_NOTIFIER_ENABLED=1`` and no URL and simply stop notifying — the
    closed-and-silent failure ``src/core/notifier_client`` exists to prevent.
    """
    lines = [line.strip() for line in REPO_UNIT.read_text().splitlines()]
    assert f"EnvironmentFile={NOTIFIER_ENV_FILE}" in lines, (
        f"deploy/watcher.service must load {NOTIFIER_ENV_FILE} (#278)"
    )
    assert f"EnvironmentFile=-{NOTIFIER_ENV_FILE}" not in lines, (
        "the notifier env file must be required, not optional — an absent file "
        "has to fail the start, not silently disable notifications"
    )


def test_repo_unit_loads_the_notifier_env_file_last() -> None:
    """Nothing loaded afterwards may override the production credential.

    ``/home/exedev/watcher/.env`` is repo-local and git-ignored — precisely the
    file a developer edits — and systemd applies env files in order. Loading it
    after the credential would let a stray ``WATCHER_NOTIFIER_BASE_URL`` there
    redirect production's notifications.
    """
    env_files = [
        line.strip()
        for line in REPO_UNIT.read_text().splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]
    assert env_files[-1] == f"EnvironmentFile={NOTIFIER_ENV_FILE}"


def test_load_env_does_not_reach_the_notifier_env_file() -> None:
    """The shell loader must stay ignorant of the path.

    ``source scripts/load-env.sh`` is what AGENTS.md puts in front of every
    pytest run, psql and gh call. Teaching it this file would undo the split in
    one line and re-arm the exact vector #278 reported.
    """
    assert str(NOTIFIER_ENV_FILE) not in LOAD_ENV.read_text()


def test_shared_env_file_does_not_carry_the_notifier_credential() -> None:
    """The on-host half: the file every agent sources must not define it.

    Asserted against the *live* file rather than a fixture, because the file is
    hand-managed on the VM — a repo-side check would pass while the real one
    still handed out the key, which is how #278 happened in the first place.
    """
    if not SHARED_ENV_FILE.exists():
        pytest.skip(f"{SHARED_ENV_FILE} not present — not a host running the service")
    offenders = sorted(_defined_keys(SHARED_ENV_FILE).intersection(NOTIFIER_CREDENTIAL_VARS))
    assert not offenders, (
        f"{SHARED_ENV_FILE} defines {', '.join(offenders)}. That file is exported "
        "into every agent shell by scripts/load-env.sh, so the production "
        f"notifier credential is held by every process on this host. Move the "
        f"definitions to {NOTIFIER_ENV_FILE}, which only deploy/watcher.service "
        "loads. (#278)"
    )


def test_notifier_env_file_is_readable_only_by_root() -> None:
    """systemd parses ``EnvironmentFile=`` as root, so ``exedev`` needs no access.

    ``/etc/watcher/.env`` is ``640 root:exedev`` because agents genuinely read
    it. This file exists so that they cannot: mode ``600`` and owner ``root``
    mean the credential is unreachable from the account the suites, the dev
    server and every agent shell run as.

    Mode is read with ``stat``, never the contents — this test has no business
    holding the key it guards.
    """
    if not SHARED_ENV_FILE.exists():
        pytest.skip(f"{SHARED_ENV_FILE} not present — not a host running the service")
    assert NOTIFIER_ENV_FILE.exists(), (
        f"{NOTIFIER_ENV_FILE} is missing on a host that runs the service — "
        "watcher.service requires it and will fail to start. (#278)"
    )
    info = NOTIFIER_ENV_FILE.stat()
    assert info.st_uid == 0, f"{NOTIFIER_ENV_FILE} must be owned by root"
    assert not info.st_mode & (stat.S_IRWXG | stat.S_IRWXO), (
        f"{NOTIFIER_ENV_FILE} is group- or world-accessible "
        f"({stat.filemode(info.st_mode)}); the whole point of the split is that "
        "the exedev account cannot read the production notifier credential (#278)"
    )
