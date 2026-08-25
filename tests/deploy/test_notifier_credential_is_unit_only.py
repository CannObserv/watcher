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

from src.core.notifier_client.client import NOTIFIER_ENV_FILE as NOTIFIER_ENV_FILE_PATH

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_UNIT = REPO_ROOT / "deploy" / "watcher.service"
LOAD_ENV = REPO_ROOT / "scripts" / "load-env.sh"

SHARED_ENV_FILE = Path("/etc/watcher/.env")

#: Imported rather than spelled again (CR-4). Four things name this path — the
#: unit, this guard, the docs, and ``NotifierCredentialMissing``'s message, which
#: is what an operator reads when the file fails to load. Moving the file with a
#: literal here would fail this module (good) and leave that message pointing at
#: a path that no longer exists (silent), so the guard and the message share one
#: source.
NOTIFIER_ENV_FILE = Path(NOTIFIER_ENV_FILE_PATH)

#: Every file a shell can pick the credential up from. ``scripts/load-env.sh``
#: exports the first two in order; the glob catches the third case, which is not
#: hypothetical — #278's own fix took a ``cp -a`` backup of the shared file
#: before editing it, and ``cp -a`` preserved ``640 root:exedev``, so the
#: production key was readable by every agent on the VM again within a minute of
#: being removed from it (CR-1).
SHARED_ENV_GLOB = ".env*"

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


def _credential_offenders(path: Path) -> list[str]:
    """Return the credential variables ``path`` defines, or [] if unreadable.

    Unreadable counts as clean deliberately: a file this account cannot read is
    a file this account cannot pick a credential up from, which is the property
    under test. ``/etc/watcher/notifier.env`` is exactly that, and it must not
    fail its own sibling guard.
    """
    try:
        return sorted(_defined_keys(path).intersection(NOTIFIER_CREDENTIAL_VARS))
    except (PermissionError, UnicodeDecodeError):
        return []


def test_no_shell_readable_env_file_carries_the_notifier_credential() -> None:
    """The on-host half: nothing a shell can read may define the pair.

    Asserted against the *live* files rather than a fixture, because they are
    hand-managed on the VM — a repo-side check would pass while the real one
    still handed out the key, which is how #278 happened in the first place.

    Three surfaces, not one (CR-1, CR-5):

    * ``/etc/watcher/.env`` — exported by ``scripts/load-env.sh``.
    * its ``.env*`` siblings — a ``.env.bak`` from an edit is not sourced by
      anything, but ``cp -a`` preserves ``640 root:exedev``, so any agent can
      simply read it. #278's own fix left one for four minutes.
    * the repo ``.env`` — the *second* file ``load-env.sh`` exports, and an
      ``EnvironmentFile=`` in the unit besides. Dropping the key there to debug
      a dispatch re-arms #278 exactly, and the original guard stayed green.
    """
    if not SHARED_ENV_FILE.exists():
        pytest.skip(f"{SHARED_ENV_FILE} not present — not a host running the service")

    candidates = sorted(SHARED_ENV_FILE.parent.glob(SHARED_ENV_GLOB))
    repo_env = REPO_ROOT / ".env"
    if repo_env.is_file():
        candidates.append(repo_env)

    offenders = {
        str(path): found
        for path in candidates
        if path.resolve() != NOTIFIER_ENV_FILE.resolve() and (found := _credential_offenders(path))
    }
    assert not offenders, (
        "these shell-readable files define the production notifier credential: "
        + "; ".join(f"{path} → {', '.join(names)}" for path, names in sorted(offenders.items()))
        + f". Anything readable as `exedev` is held by every agent, suite and REPL on "
        f"this host. The pair belongs in {NOTIFIER_ENV_FILE} and nowhere else; a "
        "backup taken while editing counts, and must be shredded rather than left "
        "beside the file it copied. (#278)"
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
