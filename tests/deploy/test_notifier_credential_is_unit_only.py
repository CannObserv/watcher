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
same way ``test_installed_unit_matches_repo`` does. The repo ``.env`` check is
deliberately *not* one of them — it guards a file that travels with the clone.
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


def _credential_candidates(system_dir: Path, repo_env: Path) -> list[Path]:
    """Every regular file a shell could read the credential out of.

    The whole directory, not a ``.env*`` glob (CR-9). The glob matched the shape
    that prompted CR-1 — a ``.env.bak-*`` beside the shared file — and missed the
    likelier one: ``notifier.env.bak`` is what a copy of the *credential file
    itself* gets called, and it does not start with ``.env``. A planted one at
    mode 644 holding the real key passed the guard. Scanning the directory costs
    nothing (five files here) and needs no prediction about what a backup will be
    named.

    Directories are dropped rather than read (CR-13): ``read_text()`` on one
    raises ``IsADirectoryError``, which would error the guard instead of skipping
    an entry that cannot hold an assignment anyway.

    ``NOTIFIER_ENV_FILE`` is excluded by resolved path — it is the one file that
    is *supposed* to define the pair.
    """
    candidates = (
        [path for path in sorted(system_dir.iterdir()) if path.is_file()]
        if system_dir.is_dir()
        else []
    )
    if repo_env.is_file():
        candidates.append(repo_env)
    return [path for path in candidates if path.resolve() != NOTIFIER_ENV_FILE.resolve()]


class TestCredentialCandidates:
    """The candidate set is the guard's real contract, so it is tested directly.

    The sweep below can only ever assert about this host's live files, which are
    (correctly) clean — so on a green VM it exercises no selection logic at all.
    These pin the selection itself against a fixture, which is how CR-9's hole
    would have been caught the first time.
    """

    def test_a_backup_of_the_credential_file_is_a_candidate(self, tmp_path) -> None:
        """The CR-9 hole: ``notifier.env.bak`` does not match ``.env*``."""
        (tmp_path / "notifier.env.bak").write_text("WATCHER_NOTIFIER_API_KEY=nk_x\n")
        names = [p.name for p in _credential_candidates(tmp_path, tmp_path / "absent")]
        assert "notifier.env.bak" in names

    def test_the_credential_file_itself_is_not_a_candidate(self) -> None:
        candidates = _credential_candidates(NOTIFIER_ENV_FILE.parent, REPO_ROOT / ".env")
        assert NOTIFIER_ENV_FILE.resolve() not in [p.resolve() for p in candidates]

    def test_directories_are_skipped(self, tmp_path) -> None:
        """CR-13: a directory would raise IsADirectoryError, not read as clean."""
        (tmp_path / ".env.d").mkdir()
        (tmp_path / ".env").write_text("DATABASE_URL=x\n")
        names = [p.name for p in _credential_candidates(tmp_path, tmp_path / "absent")]
        assert names == [".env"]

    def test_the_repo_env_file_joins_the_system_directory(self, tmp_path) -> None:
        repo_env = tmp_path / "repo.env"
        repo_env.write_text("GH_TOKEN=x\n")
        (tmp_path / "system").mkdir()
        assert _credential_candidates(tmp_path / "system", repo_env) == [repo_env]

    def test_a_missing_system_directory_yields_only_the_repo_file(self, tmp_path) -> None:
        repo_env = tmp_path / "repo.env"
        repo_env.write_text("GH_TOKEN=x\n")
        assert _credential_candidates(tmp_path / "nope", repo_env) == [repo_env]


class TestRepoEnvCandidates:
    """The repo root gets the same treatment as the system directory (#280).

    The two halves were asymmetric: ``/etc/watcher`` was scanned whole after
    CR-9, while the repo half checked ``.env`` and nothing beside it. The
    notifier VM cutover then left a ``.env.bak-precutover-*`` in this very
    directory holding every token the real file holds — the exact shape CR-1
    reported, in the one location the sweep had stopped looking.

    Unlike ``/etc/watcher``, the repo root is not a dedicated env directory —
    scanning all of it would read pyproject.toml, the lockfile and every
    top-level doc, and a placeholder assignment in one of those would fail the
    guard for no reason. So the selection is by env-file *shape*: a name
    starting with ``.env`` or ``notifier.env`` covers both families CR-9
    identified without reading anything else.
    """

    def test_the_repo_env_file_is_a_candidate(self, tmp_path) -> None:
        (tmp_path / ".env").write_text("GH_TOKEN=x\n")
        assert [p.name for p in _repo_env_candidates(tmp_path)] == [".env"]

    def test_a_backup_beside_the_repo_env_file_is_a_candidate(self, tmp_path) -> None:
        """The #280 shape: the cutover's own backup, in the repo root."""
        (tmp_path / ".env").write_text("GH_TOKEN=x\n")
        (tmp_path / ".env.bak-precutover-20260828T183354Z").write_text(
            "WATCHER_NOTIFIER_API_KEY=nk_x\n"
        )
        names = [p.name for p in _repo_env_candidates(tmp_path)]
        assert ".env.bak-precutover-20260828T183354Z" in names

    def test_a_copy_of_the_credential_file_is_a_candidate(self, tmp_path) -> None:
        """CR-9's other family, which does not start with ``.env``."""
        (tmp_path / "notifier.env.bak").write_text("WATCHER_NOTIFIER_API_KEY=nk_x\n")
        names = [p.name for p in _repo_env_candidates(tmp_path)]
        assert "notifier.env.bak" in names

    def test_ordinary_repo_files_are_not_read(self, tmp_path) -> None:
        """The reason this half is shape-selected rather than scanned whole."""
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / "README.md").write_text("WATCHER_NOTIFIER_API_KEY=<paste yours>\n")
        assert _repo_env_candidates(tmp_path) == []

    def test_directories_are_skipped(self, tmp_path) -> None:
        """Same reason as the system half: ``read_text()`` on one raises."""
        (tmp_path / ".env.d").mkdir()
        (tmp_path / ".env").write_text("GH_TOKEN=x\n")
        assert [p.name for p in _repo_env_candidates(tmp_path)] == [".env"]

    def test_a_repo_without_an_env_file_yields_nothing(self, tmp_path) -> None:
        assert _repo_env_candidates(tmp_path) == []


#: Filename families an env file's contents can hide behind in the repo root.
#: ``.env`` and its backups; ``notifier.env`` and its copies (CR-9's other
#: family, which does not start with ``.env``).
ENV_FILE_PREFIXES = (".env", "notifier.env")


def _repo_env_candidates(repo_root: Path) -> list[Path]:
    """Every env-shaped regular file in the repo root (#280).

    The system half scans ``/etc/watcher`` whole, because that directory holds
    nothing but env files and credentials. The repo root holds the project, so
    this half selects by name shape instead: scanning it whole would read the
    lockfile and every top-level doc, and a placeholder assignment in a README
    would fail the guard over nothing real.

    Selection is by *prefix family*, not by exact name — that is what CR-9's
    hole cost: a rule naming one file misses the backup beside it, which is
    precisely what the notifier VM cutover then left here.
    """
    if not repo_root.is_dir():
        return []
    return [
        path
        for path in sorted(repo_root.iterdir())
        if path.is_file() and path.name.startswith(ENV_FILE_PREFIXES)
    ]


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


def _offender_report(candidates: list[Path]) -> str:
    """Render the files that define the credential, by name and never by value."""
    offenders = {str(path): found for path in candidates if (found := _credential_offenders(path))}
    if not offenders:
        return ""
    return (
        "these shell-readable files define the production notifier credential: "
        + "; ".join(f"{path} → {', '.join(names)}" for path, names in sorted(offenders.items()))
        + f". Anything readable as `exedev` is held by every agent, suite and REPL. "
        f"The pair belongs in {NOTIFIER_ENV_FILE} and nowhere else; a backup taken "
        "while editing counts, and must be shredded rather than left beside the file "
        "it copied. (#278)"
    )


def test_no_file_in_the_system_env_directory_carries_the_notifier_credential() -> None:
    """The on-host half: nothing beside the credential file may define the pair.

    Asserted against the *live* directory rather than a fixture, because it is
    hand-managed on the VM — a repo-side check would pass while the real one
    still handed out the key, which is how #278 happened in the first place.

    Two surfaces here (CR-1, CR-5), and the repo ``.env`` is the third, split
    into its own test below so it is not skipped off-VM:

    * ``/etc/watcher/.env`` — exported by ``scripts/load-env.sh``.
    * every sibling beside it — a backup from an edit is sourced by nothing, but
      ``cp -a`` preserves ``640 root:exedev``, so any agent can simply read it.
      #278's own fix left one for four minutes.
    """
    if not SHARED_ENV_FILE.exists():
        pytest.skip(f"{SHARED_ENV_FILE} not present — not a host running the service")

    report = _offender_report(_credential_candidates(SHARED_ENV_FILE.parent, Path("/nonexistent")))
    assert not report, report


def test_no_env_shaped_file_in_the_repo_root_carries_the_notifier_credential() -> None:
    """The repo half, which must run wherever the file does (CR-10).

    ``.env`` is the *second* file ``load-env.sh`` exports and an
    ``EnvironmentFile=`` in the unit besides, so a key pasted there to debug a
    dispatch re-arms #278 exactly. It is also a repo-level surface: tying its
    check to ``/etc/watcher/.env``'s existence — as the combined sweep did —
    made it inert on every clone that is not this VM, which is precisely where
    someone edits ``.env`` without a production service to think about.

    Widened past ``.env`` itself in #280. The system half has scanned its whole
    directory since CR-9, on the reasoning that a backup is not a name you can
    predict; this half kept checking one file until the notifier VM cutover
    left a ``.env.bak-precutover-*`` right here, holding every token ``.env``
    holds. Same lesson, second location.
    """
    candidates = _repo_env_candidates(REPO_ROOT)
    if not candidates:
        pytest.skip(f"no env-shaped file in {REPO_ROOT} — nothing to check")

    report = _offender_report(candidates)
    assert not report, report


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
