"""Tests for ``scripts/dev_server.sh``.

The dev server (port 8001) previously had no launch script — AGENTS.md and
docs/COMMANDS.md documented a raw ``uvicorn`` recipe that sourced
``/etc/watcher/.env`` and therefore inherited ``DATABASE_URL`` pointing at
**production** (#233, the archiver#98 incident ported). Worse than archiver's
case: watcher's lifespan starts an embedded Procrastinate worker, so the
"dev" server was also a second worker consuming the production task queue.

``scripts/dev_server.sh`` closes that hole: it resolves a non-production
database URL, refuses to start if the resolution lands on production, and only
then execs uvicorn. This mirrors the conftest pin, which protects pytest but
not a hand-run server.

``WATCHER_DEV_SERVER_DRY_RUN=1`` makes the script print its resolution and
exit before exec'ing uvicorn, so the guard is testable without binding a port.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_server.sh"
LOADER = SCRIPT.parent / "load-env.sh"

# Distinctive, so a leak test cannot be satisfied by the role or database name
# happening to be spelled "watcher" too.
PASSWORD = "pw-8f3a1c"
PROD_URL = f"postgresql+asyncpg://watcher:{PASSWORD}@localhost:5432/watcher"
TEST_URL = f"postgresql+asyncpg://watcher:{PASSWORD}@localhost:5432/watcher_test"
DEV_URL = f"postgresql+asyncpg://watcher:{PASSWORD}@localhost:5432/watcher_dev"

# What the script may print: the same URL with the password replaced. The
# expected strings are spelled out rather than derived, so a test cannot agree
# with a broken redaction (#296 step 25).
TEST_URL_SHOWN = "postgresql+asyncpg://watcher:***@localhost:5432/watcher_test"
DEV_URL_SHOWN = "postgresql+asyncpg://watcher:***@localhost:5432/watcher_dev"


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the script in dry-run mode with a hermetic environment.

    ``WATCHER_DEV_SERVER_SKIP_ENV_FILES`` stops the script loading the real
    ``/etc/watcher/.env`` and ``.env``, so tests control resolution entirely.
    """
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "WATCHER_DEV_SERVER_DRY_RUN": "1",
            "WATCHER_DEV_SERVER_SKIP_ENV_FILES": "1",
            **env,
        },
        text=True,
        capture_output=True,
    )


def test_resolves_to_test_database_by_default() -> None:
    """With only TEST_DATABASE_URL set, the dev server targets the test DB."""
    result = run({"TEST_DATABASE_URL": TEST_URL, "DATABASE_URL": PROD_URL})
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={TEST_URL_SHOWN}" in result.stdout


def test_dedicated_dev_url_wins_over_test_url() -> None:
    """WATCHER_DEV_DATABASE_URL takes precedence when both are set.

    Lets an operator keep a persistent dev database that pytest's table
    teardown will not wipe out from under a running dev server.
    """
    result = run(
        {
            "WATCHER_DEV_DATABASE_URL": DEV_URL,
            "TEST_DATABASE_URL": TEST_URL,
            "DATABASE_URL": PROD_URL,
        }
    )
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={DEV_URL_SHOWN}" in result.stdout


def test_refuses_when_resolution_equals_production() -> None:
    """The archiver#98 failure shape: dev server resolving onto production."""
    result = run({"DATABASE_URL": PROD_URL, "TEST_DATABASE_URL": PROD_URL})
    assert result.returncode != 0
    assert "production" in result.stderr.lower()
    assert "watcher" in result.stderr


def test_refuses_when_no_non_production_url_is_available() -> None:
    """Absent a dev/test URL the script must fail, never fall back to prod."""
    result = run({"DATABASE_URL": PROD_URL})
    assert result.returncode != 0
    assert "TEST_DATABASE_URL" in result.stderr


def test_clears_inherited_procrastinate_url() -> None:
    """PROCRASTINATE_DATABASE_URL must not survive into the child environment.

    ``src/workers/__init__.py`` consults it before DATABASE_URL; leaving a
    production value in place would run the embedded worker against the
    production task queue even with a test DATABASE_URL.
    """
    result = run({"PROCRASTINATE_DATABASE_URL": PROD_URL, "TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert "PROCRASTINATE_DATABASE_URL=(cleared)" in result.stdout


def test_overrides_inherited_migration_url() -> None:
    """WATCHER_MIGRATION_DATABASE_URL must be forced onto the dev database.

    #259 gives Alembic its own credential, and ``/etc/watcher/.env`` — which
    this script loads — is where it lives. The script runs ``alembic upgrade
    head`` before exec'ing uvicorn, so an inherited production value would make
    the *dev* launch path migrate **production** with the schema owner's
    rights: the #233 hazard with the one variable that can drop tables.
    """
    result = run({"WATCHER_MIGRATION_DATABASE_URL": PROD_URL, "TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    # Line-exact: PROD_URL is a prefix of TEST_URL, so a substring check on the
    # whole report would pass on the very output it is meant to reject.
    assert f"WATCHER_MIGRATION_DATABASE_URL={TEST_URL_SHOWN}" in result.stdout.splitlines()


def test_migration_url_follows_the_dedicated_dev_database() -> None:
    """It tracks the resolved dev URL, not TEST_DATABASE_URL specifically."""
    result = run({"WATCHER_DEV_DATABASE_URL": DEV_URL, "TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert f"WATCHER_MIGRATION_DATABASE_URL={DEV_URL_SHOWN}" in result.stdout


def test_refuses_to_bind_the_production_port() -> None:
    """Port 8000 belongs to systemd; a dev launch there is always a mistake."""
    result = run({"TEST_DATABASE_URL": TEST_URL, "WATCHER_DEV_PORT": "8000"})
    assert result.returncode != 0
    assert "8000" in result.stderr


def test_refuses_production_database_name_despite_differing_url_string() -> None:
    """String equality is defeated by cosmetic URL differences.

    ``postgresql://…/watcher`` and ``postgresql+asyncpg://…/watcher`` are
    different strings naming the same database.
    """
    result = run(
        {
            "DATABASE_URL": "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher",
            "TEST_DATABASE_URL": "postgresql://watcher:watcher@localhost:5432/watcher",
        }
    )
    assert result.returncode != 0
    assert "watcher" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://watcher:watcher@127.0.0.1:5432/watcher",
        "postgresql://u:p@otherhost:5432/watcher",
        "postgresql://u:p@localhost:5432/test_watcher",
        "postgresql://u:p@localhost:5432/watcher_testing",
    ],
)
def test_requires_a_test_or_dev_database_name(url: str) -> None:
    """Positive assertion: the dev DB name must carry a _test/_dev suffix.

    Host spelling and driver prefix are not a safety boundary; the database
    name is. ``test_watcher`` and ``watcher_testing`` are near-misses that
    a substring check would wrongly accept.
    """
    result = run({"TEST_DATABASE_URL": url})
    assert result.returncode != 0
    assert "_test" in result.stderr


@pytest.mark.parametrize("suffix", ["_test", "_dev"])
def test_accepts_test_and_dev_suffixed_names(suffix: str) -> None:
    url = f"postgresql+asyncpg://watcher:{PASSWORD}@localhost:5432/watcher{suffix}"
    shown = f"postgresql+asyncpg://watcher:***@localhost:5432/watcher{suffix}"
    result = run({"TEST_DATABASE_URL": url, "DATABASE_URL": PROD_URL})
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={shown}" in result.stdout


def make_repo(root: Path) -> Path:
    """A throwaway REPO_ROOT: the script and its loader under ``root/scripts``.

    Returns the copied script. The loader travels with it because the script
    finds it beside itself, never on the real checkout's path.
    """
    (root / "scripts").mkdir(parents=True)
    for src in (SCRIPT, LOADER):
        (root / "scripts" / src.name).write_bytes(src.read_bytes())
    return root / "scripts" / SCRIPT.name


def run_loading(script: Path, system_env: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Dry-run *script* with its env files loaded, not skipped.

    ``WATCHER_SYSTEM_ENV_FILE`` points the loader at a ``tmp_path`` file, so no
    test reads the real ``/etc/watcher/.env`` (#407). The project file is left
    to the loader's own resolution, which is what these tests exercise.
    """
    return subprocess.run(
        ["bash", str(script)],
        cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin",
            "WATCHER_DEV_SERVER_DRY_RUN": "1",
            "WATCHER_SYSTEM_ENV_FILE": str(system_env),
        },
        text=True,
        capture_output=True,
    )


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_loads_env_files_when_not_skipped(tmp_path: Path) -> None:
    """Cover the env-file loading path, not just the skip flag.

    ``WATCHER_DEV_SERVER_SKIP_ENV_FILES`` exists only for tests, so without
    this case the real-world resolution path — read .env, then guard — is never
    exercised. Runs the script against a throwaway repo root whose .env
    supplies TEST_DATABASE_URL.
    """
    script = make_repo(tmp_path / "repo")
    (tmp_path / "repo" / ".env").write_text(f"TEST_DATABASE_URL={TEST_URL}\n")

    result = run_loading(script, tmp_path / "no-system.env", cwd=tmp_path / "repo")
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={TEST_URL_SHOWN}" in result.stdout.splitlines()


def test_system_file_loads_before_the_project_file(tmp_path: Path) -> None:
    """The loader's order: a repo-local value wins over the system file's."""
    script = make_repo(tmp_path / "repo")
    system = tmp_path / "system.env"
    system.write_text(f"TEST_DATABASE_URL={DEV_URL}\nWATCHER_DEV_PORT=8123\n")
    (tmp_path / "repo" / ".env").write_text(f"TEST_DATABASE_URL={TEST_URL}\n")

    result = run_loading(script, system, cwd=tmp_path / "repo")
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={TEST_URL_SHOWN}" in result.stdout.splitlines()
    assert "PORT=8123" in result.stdout.splitlines()


@pytest.mark.parametrize("shape", ["$(touch {m})", "`touch {m}`"])
def test_a_command_substitution_in_an_env_file_is_never_run(tmp_path: Path, shape: str) -> None:
    """#407: the env files are parsed, never sourced.

    Sourced, a ``$(…)`` in a secrets file runs as shell code in the dev
    server's launch path. Parsed, it is a string: the value reaches the report
    as written and the command never runs.
    """
    marker = tmp_path / "ran"
    value = shape.format(m=marker)
    script = make_repo(tmp_path / "repo")
    (tmp_path / "repo" / ".env").write_text(
        f"TEST_DATABASE_URL={TEST_URL}\nWATCHER_DEV_PORT={value}\n"
    )

    result = run_loading(script, tmp_path / "no-system.env", cwd=tmp_path / "repo")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert f"PORT={value}" in result.stdout.splitlines()


def test_a_cwd_outside_the_repo_still_loads_the_repos_env(tmp_path: Path) -> None:
    """The loader resolves ``.env`` from the cwd's checkout, not the script's.

    So the script ``cd``s to REPO_ROOT before loading: run from anywhere else,
    it would read the wrong ``.env`` or none. The decoy here names a database
    the guard accepts, so only the report's database name can tell them apart.
    """
    script = make_repo(tmp_path / "repo")
    (tmp_path / "repo" / ".env").write_text(f"TEST_DATABASE_URL={TEST_URL}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text(f"TEST_DATABASE_URL={DEV_URL}\n")

    result = run_loading(script, tmp_path / "no-system.env", cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={TEST_URL_SHOWN}" in result.stdout.splitlines()


def test_a_linked_worktree_without_env_loads_the_primarys(tmp_path: Path) -> None:
    """#404 through the dev server: the git-ignored ``.env`` lives only in the
    primary checkout, so a linked worktree's launch falls back to it. Run from
    outside both checkouts, so the script's own ``cd`` is what finds the
    worktree."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    primary, linked = tmp_path / "primary", tmp_path / "linked"
    make_repo(primary)
    _git("init", "-q", cwd=primary)
    _git("add", "scripts", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    _git("worktree", "add", "-q", "-b", "wt", str(linked), cwd=primary)
    (primary / ".env").write_text(f"TEST_DATABASE_URL={TEST_URL}\n")
    assert not (linked / ".env").exists()

    result = run_loading(linked / "scripts" / SCRIPT.name, tmp_path / "no-system.env", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={TEST_URL_SHOWN}" in result.stdout.splitlines()


def test_no_loaded_value_reaches_stdout_or_stderr(tmp_path: Path) -> None:
    """The report is pasted into issues; the env files hold every secret.

    Each value below carries a sentinel no report line has any reason to
    print. The URLs the report does name come back redacted or replaced.
    """
    sys_secret, proj_secret = "sys-9d2e71", "proj-4b8c06"
    system = tmp_path / "system.env"
    system.write_text(
        f"DATABASE_URL=postgresql+asyncpg://watcher_app:{sys_secret}@localhost:5432/watcher\n"
        f"WATCHER_MIGRATION_DATABASE_URL=postgresql://watcher:{sys_secret}@localhost/watcher\n"
        f"WATCHER_BUS_REDIS_URL=redis://watcher:{sys_secret}@broker:6379/0\n"
    )
    script = make_repo(tmp_path / "repo")
    (tmp_path / "repo" / ".env").write_text(
        f"TEST_DATABASE_URL={TEST_URL}\nGH_TOKEN={proj_secret}\n"
    )

    result = run_loading(script, system, cwd=tmp_path / "repo")
    assert result.returncode == 0, result.stderr
    for secret in (sys_secret, proj_secret, PASSWORD):
        assert secret not in result.stdout + result.stderr, secret
    assert f"DATABASE_URL={TEST_URL_SHOWN}" in result.stdout.splitlines()


def test_loads_through_the_loader_and_no_env_file() -> None:
    """#407: one loader for every hand-run path. The script names neither env
    file, so a second, sourcing copy of the load cannot come back."""
    text = SCRIPT.read_text()
    code = [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert '. "$REPO_ROOT/scripts/load-env.sh"' in code
    assert not [line for line in code if "/etc/watcher/.env" in line or '/.env"' in line]


#: Where a hand-run recipe lives. docs/plans/ and the dated rehearsal log are
#: history, quoted as it was run.
SOURCING_SCOPE_GLOBS = ("scripts/**/*", "docs/**/*.md", "AGENTS.md", "README.md")
SOURCING_SCOPE_EXCLUDED = ("docs/plans/", "docs/RECOVERY-REHEARSALS.md")

#: ``set -a`` (or ``set -o allexport``), then — past comment lines and an
#: ``[ -f … ] &&`` guard — a ``.`` or ``source``: sourcing a file to export what
#: it defines.
SET_A_SOURCE = re.compile(
    r"set -(?:a|o allexport)\s*(?:;|&&|\n)(?:[ \t]*#[^\n]*\n)*\s*"
    r"(?:\[[^\]\n]*\]\s*&&\s*)?(?:\.|source)\s"
)


@pytest.mark.parametrize(
    "form",
    [
        "set -a; . /etc/watcher/backup.env; set +a",
        "set -a\n  # shellcheck disable=SC1091\n  [ -f /etc/watcher/.env ] && . /etc/watcher/.env",
        "set -a\nsource .env\nset +a",
        "set -a && . /etc/watcher/.env && set +a",
        "set -o allexport; . /etc/watcher/.env; set +o allexport",
    ],
)
def test_the_sourcing_guard_matches_the_forms_it_replaced(form: str) -> None:
    """So the scan below cannot pass by matching nothing."""
    assert SET_A_SOURCE.search(form)


def test_no_recipe_sources_an_env_file_under_set_a() -> None:
    """#407: an env file is data. ``scripts/load-env.sh`` parses it; ``set -a;
    . file`` runs it as shell code, and exports every name to whatever follows.
    Runbook one-liners take the loader with ``WATCHER_SYSTEM_ENV_FILE`` and
    ``WATCHER_PROJECT_ENV_FILE`` pointed at the one file they mean."""
    root = SCRIPT.parents[1]
    paths = {
        p
        for glob in SOURCING_SCOPE_GLOBS
        for p in root.glob(glob)
        if p.is_file()
        and not any(p.relative_to(root).as_posix().startswith(x) for x in SOURCING_SCOPE_EXCLUDED)
    }
    assert root / "scripts" / "dev_server.sh" in paths
    offenders = [
        p.relative_to(root).as_posix()
        for p in sorted(paths)
        if SET_A_SOURCE.search(p.read_text(errors="replace"))
    ]
    assert not offenders, f"set -a sources an env file in: {offenders}"


def test_reports_planned_migration_of_the_dev_database() -> None:
    """The script migrates the dev DB before serving.

    Watcher's tests create/drop tables directly, so a dev server pointed at
    the test database routinely starts against a partial schema and 500s on
    every write. An operator who hits that is one step from reaching for the
    old prod-pointing recipe, so the launch path has to leave the dev database
    usable on its own.
    """
    result = run({"TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert f"MIGRATE={TEST_URL_SHOWN}" in result.stdout


def test_test_database_fallback_is_rebuilt_from_scratch() -> None:
    """The TEST_DATABASE_URL fallback resets the public schema before migrating.

    pytest builds watcher_test with ``Base.metadata.create_all``, not alembic,
    so its ``alembic_version`` (if any) never matches the actual tables and a
    plain ``upgrade head`` fails mid-history. The test DB is disposable by
    definition, so the launch path drops and recreates the ``public`` schema
    to get a deterministic, migration-built state.
    """
    result = run({"TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert "RESET=public-schema" in result.stdout


def test_persistent_dev_database_is_not_reset() -> None:
    """An explicit WATCHER_DEV_DATABASE_URL keeps its data across launches.

    The whole point of the dedicated dev DB is persistence; it is expected to
    be alembic-managed, so it gets a plain ``upgrade head`` and no schema drop.
    """
    result = run({"WATCHER_DEV_DATABASE_URL": DEV_URL, "TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert "RESET=(none)" in result.stdout
    assert f"MIGRATE={DEV_URL_SHOWN}" in result.stdout


def test_migration_can_be_skipped() -> None:
    """WATCHER_DEV_SKIP_MIGRATE=1 leaves the dev schema untouched, reset included."""
    result = run({"TEST_DATABASE_URL": TEST_URL, "WATCHER_DEV_SKIP_MIGRATE": "1"})
    assert result.returncode == 0, result.stderr
    assert "MIGRATE=(skipped)" in result.stdout
    assert "RESET=(none)" in result.stdout
    assert "PROCRASTINATE_SCHEMA=(skipped)" in result.stdout


def test_reset_branch_always_reapplies_the_procrastinate_schema() -> None:
    """#341: the public-schema drop takes procrastinate's tables with it.

    No migration creates them — procrastinate owns its own DDL — so a launch
    that resets and then only runs alembic boots a worker whose
    ``register_worker`` raises ``UndefinedTable`` at once, and every check,
    drain, reap and periodic publish silently never runs.
    """
    result = run({"TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert "PROCRASTINATE_SCHEMA=apply" in result.stdout.splitlines()


def test_persistent_branch_applies_the_procrastinate_schema_only_when_missing() -> None:
    """``schema --apply`` is not idempotent over an existing schema, so a
    persistent dev database gets it on first launch and never again."""
    result = run({"WATCHER_DEV_DATABASE_URL": DEV_URL})
    assert result.returncode == 0, result.stderr
    assert "PROCRASTINATE_SCHEMA=apply-if-missing" in result.stdout.splitlines()


def test_schema_apply_runs_after_the_migration() -> None:
    """The dry run exits before the executed path, so read the source: the
    apply must follow ``alembic upgrade head`` (the reset leaves nothing for it
    to collide with, and the persistent branch's probe must see the migrated
    database) and go through the worker's own app, which connects where the
    embedded worker will."""
    text = SCRIPT.read_text()
    migrate = text.index("uv run alembic upgrade head")
    apply = text.index("uv run procrastinate --app=src.workers.app schema --apply")
    assert migrate < apply < text.index("exec uv run uvicorn")


#: The unit's ExecStartPre form (deploy/watcher.service), run from REPO_ROOT (#358).
CHECK_ARGV = "sync --locked --offline"


def run_live(
    tmp_path: Path, check_exit: int, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the script past the dry-run exit, against stub ``uv`` and ``psql``.

    The dry run exits before the ``cd`` and every ``uv`` call, so only the
    executed path can show the check runs first (#401). Each stub appends its
    argv to a log and exits 0; ``uv`` given the check prints uv's own error
    and exits *check_exit*, the way an offline ``uv sync`` that needs a download
    does (#358, measured under ``unshare -n``).
    """
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    (stubs / "uv").write_text(
        "#!/bin/bash\n"
        f'echo "uv $*" >> {log}\n'
        f'if [[ "$*" == "{CHECK_ARGV}" ]]; then\n'
        f"  if [[ {check_exit} == 0 ]]; then echo 'Checked 83 packages' >&2; exit 0; fi\n"
        "  echo 'error: Failed to download `co-core-aio==0.37.0`' >&2\n"
        f"  exit {check_exit}\n"
        "fi\n"
    )
    (stubs / "psql").write_text(f'#!/bin/bash\necho "psql $*" >> {log}\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": f"{stubs}:/usr/bin:/bin",
            "WATCHER_DEV_SERVER_SKIP_ENV_FILES": "1",
            **env,
        },
        text=True,
        capture_output=True,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_check_argv_is_the_units_command() -> None:
    """#401, #358: one form of the check is in use. The unit runs uv by absolute
    path from its WorkingDirectory; the script runs it from REPO_ROOT."""
    unit = (SCRIPT.parents[1] / "deploy" / "watcher.service").read_text()
    pre = [line for line in unit.splitlines() if line.startswith("ExecStartPre=")]
    assert f"ExecStartPre=/usr/local/bin/uv {CHECK_ARGV}" in pre


def test_dry_run_reports_the_lock_check() -> None:
    """#401, #358: the dry run says the check will run, with the rest of the resolution.

    It runs on every launch, migration skipped or not: the final ``uv run
    uvicorn`` syncs the venv too.
    """
    for extra in ({}, {"WATCHER_DEV_SKIP_MIGRATE": "1"}):
        result = run({"TEST_DATABASE_URL": TEST_URL, **extra})
        assert result.returncode == 0, result.stderr
        assert "LOCK_CHECK=run" in result.stdout.splitlines()


def test_lock_check_runs_before_any_other_command(tmp_path: Path) -> None:
    """#401: every later ``uv run`` syncs the venv, which from the deploy
    checkout is the service's own, so the unit's check goes first: before the
    reset too, which a refusal would otherwise leave half done."""
    result, calls = run_live(tmp_path, 0, {"TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert calls[0] == f"uv {CHECK_ARGV}"
    assert [c.split()[0] for c in calls[1:]] == ["psql", "uv", "uv", "uv"]
    assert calls[-1].startswith("uv run uvicorn ")
    assert "Checked 83 packages" in result.stderr


def test_a_failing_lock_check_stops_the_launch(tmp_path: Path) -> None:
    """#401: a check that fails stops the script before anything else runs,
    and the refusal names the fix: the sync through the token wrapper (#358)."""
    result, calls = run_live(tmp_path, 1, {"TEST_DATABASE_URL": TEST_URL})
    assert result.returncode != 0
    assert calls == [f"uv {CHECK_ARGV}"]
    assert "error: Failed to download" in result.stderr
    assert "bash scripts/co-pypi.sh uv sync --locked" in result.stderr
    assert result.stdout == ""


def test_bus_url_is_cleared_unless_dev_bus_is_explicit() -> None:
    """An inherited production WATCHER_BUS_REDIS_URL must not reach the child (#245).

    /etc/watcher/.env carries the production bus URL; a dev server inheriting it
    would publish content.fetch commands that make the live Replicator request
    real origins — the #233 hazard, bus edition.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_BUS_REDIS_URL": "redis://localhost:6379/0",
        }
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER_BUS_REDIS_URL=(cleared)" in result.stdout
    # And the opt-in goes with it: a cleared URL plus a leaked flag would be
    # merely useless, but the pair must stay consistent so the two branches of
    # this script are the only two shapes the app ever sees (#262).
    assert "WATCHER_BUS_ENABLED=(cleared)" in result.stdout


def test_explicit_dev_bus_url_is_forwarded() -> None:
    """WATCHER_DEV_BUS_REDIS_URL opts a dev server into a (scratch) bus."""
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_BUS_REDIS_URL": "redis://localhost:6379/0",
            "WATCHER_DEV_BUS_REDIS_URL": "redis://localhost:6379/15",
        }
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER_BUS_REDIS_URL=redis://localhost:6379/15" in result.stdout


def test_dev_bus_branch_sets_the_bus_opt_in() -> None:
    """#262: a sanctioned launch path pointed at a scratch broker opts itself in.

    The flag is otherwise unit-only, so without this the dev server would set a
    dev bus URL and then refuse to start on the very gate that exists to stop
    *unsanctioned* processes — the same way this script already handles the
    production-database opt-in's dev counterpart.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_DEV_BUS_REDIS_URL": "redis://localhost:6379/15",
        }
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER_BUS_ENABLED=1" in result.stdout


def test_an_inherited_bus_opt_in_does_not_survive_without_a_dev_bus() -> None:
    """A flag leaked into an env file must not re-arm the production URL.

    The unit is the only sanctioned home for it, but this script loads
    /etc/watcher/.env and the repo .env — so it clears what it did not set.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_BUS_REDIS_URL": "redis://localhost:6379/0",
            "WATCHER_BUS_ENABLED": "1",
        }
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER_BUS_REDIS_URL=(cleared)" in result.stdout
    assert "WATCHER_BUS_ENABLED=(cleared)" in result.stdout


def test_report_never_prints_a_database_password() -> None:
    """The dry-run report is pasted into issues and scrollback (#296 step 25).

    Every URL it echoes came from an env file, so each one carries a real
    password: DATABASE_URL and WATCHER_MIGRATION_DATABASE_URL are the two role
    credentials, and the MIGRATE line repeats one of them. Printing the
    database is the point; printing the password is a leak with no reader.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_MIGRATION_DATABASE_URL": PROD_URL,
            "WATCHER_DEV_BUS_REDIS_URL": f"redis://watcher:{PASSWORD}@broker:6379/15",
        }
    )
    assert result.returncode == 0, result.stderr
    assert PASSWORD not in result.stdout
    assert PASSWORD not in result.stderr
    # …and the report still says which database and which broker, redacted.
    assert f"DATABASE_URL={TEST_URL_SHOWN}" in result.stdout
    assert "WATCHER_BUS_REDIS_URL=redis://watcher:***@broker:6379/15" in result.stdout


def test_report_leaves_a_passwordless_url_intact() -> None:
    """Redaction rewrites credentials, not URLs: a peer-auth URL is unchanged."""
    url = "postgresql+asyncpg://localhost:5432/watcher_test"
    result = run({"TEST_DATABASE_URL": url})
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={url}" in result.stdout.splitlines()


def _script_lines() -> list[tuple[int, str]]:
    return list(enumerate(SCRIPT.read_text().splitlines(), start=1))


#: The variables that hold a URL with its password, as spelled in the script.
RAW_URL_VARS = (
    "DATABASE_URL",
    "WATCHER_MIGRATION_DATABASE_URL",
    "DEV_URL",
    "WATCHER_BUS_REDIS_URL",
)


def _mentions_raw_url(line: str) -> bool:
    return any(f"${var}" in line or f"${{{var}" in line for var in RAW_URL_VARS)


def test_no_echo_prints_a_raw_url() -> None:
    """Guards the runtime lines too, which the dry run exits before reaching.

    The report is testable by running the script; ``dev_server: port 8001 →
    <url>`` and the reset/migrate lines are not, since they print on the way to
    exec'ing uvicorn. So this reads the source instead: an ``echo`` may name a
    URL variable only through ``redact``.
    """
    offenders = [
        (n, line)
        for n, line in _script_lines()
        if line.lstrip().startswith("echo ") and _mentions_raw_url(line) and "redact" not in line
    ]
    assert not offenders, f"echo prints an unredacted URL: {offenders}"
    redacted = [
        line
        for _, line in _script_lines()
        if line.lstrip().startswith("echo ") and "redact" in line
    ]
    assert redacted, "no echo goes through redact — the check would pass vacuously"


def test_no_report_variable_takes_a_raw_url() -> None:
    """A *_REPORT is printed verbatim, so assigning a raw URL to one is the
    same leak one step removed."""
    offenders = [
        (n, line)
        for n, line in _script_lines()
        if "_REPORT=" in line and _mentions_raw_url(line) and "redact" not in line
    ]
    assert not offenders, f"report variable holds an unredacted URL: {offenders}"


def test_no_command_receives_a_url_in_argv() -> None:
    """``psql "$DATABASE_URL"`` puts the password in ``ps`` output for every
    user on the box — the argv hazard docs/RECOVERY.md documents for the
    restore path, in the one script that runs it daily."""
    offenders = [
        (n, line)
        for n, line in _script_lines()
        if "psql " in line and _mentions_raw_url(line) and "without_password" not in line
    ]
    assert not offenders, f"psql is handed an unredacted URL: {offenders}"
