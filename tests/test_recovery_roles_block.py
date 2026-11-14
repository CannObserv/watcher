"""docs/RECOVERY.md → *The roles come first*, run as written (#439).

The block reads the two passwords ``/etc/watcher/.env`` already holds and
creates the roles a fresh cluster lacks. Step 1 runs ``CREATE ROLE watcher LOGIN
PASSWORD :'pw'``, and PostgreSQL 10+ takes an empty ``pw`` as *no* password,
which no ``scram-sha-256`` rule admits. A missing password must stop the block
before step 1, and the stop must not close the operator's shell.

These tests run the block's exact bytes under bash from a scratch directory,
with a stub ``scripts/load-env.sh`` (the URLs come from the test's environment,
never the real file) and a stub ``sudo`` that records each call and runs
nothing. What psql then does with step 1's guard is proven on a throwaway
cluster, as #437's was; here it is pinned as text.
"""

import re
import subprocess
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[1] / "docs" / "RECOVERY.md"

MIGRATE_URL = "postgresql+asyncpg://watcher:m-s3cret@localhost:5432/watcher"
APP_URL = "postgresql+asyncpg://watcher_app:a-s3cret@localhost:5432/watcher"

#: Records the password variables it was handed and its argv, one line a call;
#: drains stdin so a heredoc or a redirect never blocks.
STUB_SUDO = """#!/bin/bash
printf '%s|%s|%s\\n' "${WATCHER_MIGRATE_PASSWORD-}" "${WATCHER_APP_PASSWORD-}" "$*" >> "$SUDO_LOG"
cat > /dev/null
"""


def roles_block() -> str:
    """The one fenced bash block that creates the ``watcher`` role."""
    blocks = re.findall(r"^```bash\n(.*?)^```", DOC.read_text(), flags=re.S | re.M)
    [block] = [b for b in blocks if "CREATE ROLE watcher LOGIN" in b]
    return block


def run_block(tmp_path: Path, **urls: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the block, then report whether the shell survived and the passwords didn't."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "load-env.sh").write_text("# stub: the test sets the URLs\n")
    for redirected in ("setup-db-roles.sql", "setup-backup-role.sql"):
        (tmp_path / "scripts" / redirected).write_text("-- stub: sudo reads nothing\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sudo = bindir / "sudo"
    sudo.write_text(STUB_SUDO)
    sudo.chmod(0o755)
    log = tmp_path / "sudo.log"
    log.touch()
    after = 'echo "SURVIVED MIGRATE_PW=${MIGRATE_PW-unset} APP_PW=${APP_PW-unset}"\n'
    script = roles_block() + after
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "SUDO_LOG": str(log), **urls}
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, log.read_text().splitlines()


def test_both_passwords_run_every_step(tmp_path: Path) -> None:
    """So the stop cases below cannot pass by a block that never runs anything."""
    result, calls = run_block(
        tmp_path, WATCHER_MIGRATION_DATABASE_URL=MIGRATE_URL, DATABASE_URL=APP_URL
    )
    assert "STOP" not in result.stderr
    assert len(calls) == 6, calls
    assert calls[0].startswith("m-s3cret||"), "step 1 is not handed the migration password"
    argvs = [call.split("|", 2)[2] for call in calls]
    assert not [a for a in argvs if "m-s3cret" in a or "a-s3cret" in a], "a password in argv"
    assert calls[1].endswith("createdb -O watcher watcher")
    assert calls[2].startswith("|a-s3cret|"), "step 3 is not handed the app password"


@pytest.mark.parametrize(
    "urls",
    [
        pytest.param({"DATABASE_URL": APP_URL}, id="migrate-url-unset"),
        pytest.param(
            {
                "WATCHER_MIGRATION_DATABASE_URL": "postgresql+asyncpg://watcher:@localhost/watcher",
                "DATABASE_URL": APP_URL,
            },
            id="migrate-password-empty",
        ),
        pytest.param(
            {
                "WATCHER_MIGRATION_DATABASE_URL": "postgresql://watcher_migrate:m-s3cret@h/watcher",
                "DATABASE_URL": APP_URL,
            },
            id="migrate-other-role",
        ),
        pytest.param(
            {
                "WATCHER_MIGRATION_DATABASE_URL": "postgresql://watcher@localhost/watcher",
                "DATABASE_URL": APP_URL,
            },
            id="migrate-no-password",
        ),
        pytest.param({"WATCHER_MIGRATION_DATABASE_URL": MIGRATE_URL}, id="app-url-unset"),
        pytest.param(
            {"WATCHER_MIGRATION_DATABASE_URL": MIGRATE_URL, "DATABASE_URL": MIGRATE_URL},
            id="app-url-names-watcher",
        ),
    ],
)
def test_a_missing_password_stops_before_step_1(tmp_path: Path, urls: dict[str, str]) -> None:
    """#439: nothing runs once the check fails, and the shell running the block
    lives on. A URL of another shape counts as missing: the old parse kept a
    non-matching URL whole and took ``scheme://user:pw`` for the password."""
    result, calls = run_block(tmp_path, **urls)
    assert calls == []
    assert "STOP" in result.stderr
    assert "SURVIVED" in result.stdout


def test_the_passwords_end_with_the_block(tmp_path: Path) -> None:
    """Read in a subshell, so neither outlives the block in an interactive shell."""
    result, _ = run_block(
        tmp_path, WATCHER_MIGRATION_DATABASE_URL=MIGRATE_URL, DATABASE_URL=APP_URL
    )
    assert "SURVIVED MIGRATE_PW=unset APP_PW=unset" in result.stdout


def test_step_1_refuses_an_empty_password_itself() -> None:
    """Pasted alone, step 1 has no shell check in front of it: its SQL carries
    the guard from setup-db-roles.sql (#437), ahead of the CREATE ROLE."""
    sql = roles_block().split("<<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
    create = sql.index("CREATE ROLE watcher LOGIN")
    guard = sql[:create]
    assert "\\getenv pw WATCHER_MIGRATE_PASSWORD" in guard
    assert "\\if :{?pw}" in guard, "an unset variable is not caught"
    assert re.search(r":'pw' = '' AS \w+ \\gset", guard), "an empty value is not caught"
    assert "RAISE EXCEPTION" in guard, "the refusal exits 0"
