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

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_server.sh"

PROD_URL = "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher"
TEST_URL = "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher_test"
DEV_URL = "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher_dev"


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the script in dry-run mode with a hermetic environment.

    ``WATCHER_DEV_SERVER_SKIP_ENV_FILES`` stops the script sourcing the real
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
    assert f"DATABASE_URL={TEST_URL}" in result.stdout


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
    assert f"DATABASE_URL={DEV_URL}" in result.stdout


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
    this script sources — is where it lives. The script runs ``alembic upgrade
    head`` before exec'ing uvicorn, so an inherited production value would make
    the *dev* launch path migrate **production** with the schema owner's
    rights: the #233 hazard with the one variable that can drop tables.
    """
    result = run({"WATCHER_MIGRATION_DATABASE_URL": PROD_URL, "TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    # Line-exact: PROD_URL is a prefix of TEST_URL, so a substring check on the
    # whole report would pass on the very output it is meant to reject.
    assert f"WATCHER_MIGRATION_DATABASE_URL={TEST_URL}" in result.stdout.splitlines()


def test_migration_url_follows_the_dedicated_dev_database() -> None:
    """It tracks the resolved dev URL, not TEST_DATABASE_URL specifically."""
    result = run({"WATCHER_DEV_DATABASE_URL": DEV_URL, "TEST_DATABASE_URL": TEST_URL})
    assert result.returncode == 0, result.stderr
    assert f"WATCHER_MIGRATION_DATABASE_URL={DEV_URL}" in result.stdout


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
    url = f"postgresql+asyncpg://watcher:watcher@localhost:5432/watcher{suffix}"
    result = run({"TEST_DATABASE_URL": url, "DATABASE_URL": PROD_URL})
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={url}" in result.stdout


def test_sources_env_files_when_not_skipped(tmp_path: Path) -> None:
    """Cover the env-file sourcing path, not just the skip flag.

    ``WATCHER_DEV_SERVER_SKIP_ENV_FILES`` exists only for tests, so without
    this case the real-world resolution path — read .env, then guard — is never
    exercised. Runs the script against a throwaway repo root whose .env
    supplies TEST_DATABASE_URL.
    """
    (tmp_path / "scripts").mkdir()
    script_copy = tmp_path / "scripts" / "dev_server.sh"
    script_copy.write_bytes(SCRIPT.read_bytes())
    (tmp_path / ".env").write_text(f"TEST_DATABASE_URL={TEST_URL}\n")

    result = subprocess.run(
        ["bash", str(script_copy)],
        env={"PATH": "/usr/bin:/bin", "WATCHER_DEV_SERVER_DRY_RUN": "1"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"DATABASE_URL={TEST_URL}" in result.stdout


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
    assert f"MIGRATE={TEST_URL}" in result.stdout


def test_test_database_fallback_is_rebuilt_from_scratch() -> None:
    """The TEST_DATABASE_URL fallback resets the public schema before migrating.

    pytest builds watcher_test with ``Base.metadata.create_all``, not alembic,
    so its ``alembic_version`` (if any) never matches the actual tables and a
    plain ``upgrade head`` fails mid-history. The test DB is disposable by
    definition, so the launch path drops and recreates the ``public`` schema
    to get a deterministic, migration-built state. The ``information`` schema
    (Archiver's, persisted between pytest sessions) is untouched.
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
    assert f"MIGRATE={DEV_URL}" in result.stdout


def test_migration_can_be_skipped() -> None:
    """WATCHER_DEV_SKIP_MIGRATE=1 leaves the dev schema untouched, reset included."""
    result = run({"TEST_DATABASE_URL": TEST_URL, "WATCHER_DEV_SKIP_MIGRATE": "1"})
    assert result.returncode == 0, result.stderr
    assert "MIGRATE=(skipped)" in result.stdout
    assert "RESET=(none)" in result.stdout


def test_bus_url_is_cleared_unless_dev_bus_is_explicit() -> None:
    """An inherited production WATCHER_BUS_REDIS_URL must not reach the child (#245).

    /etc/watcher/.env carries the production bus URL; a dev server inheriting it
    would publish fetch-policy frames onto the stream the live Replicator paces
    real origins from — the #233 hazard, bus edition.
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

    The unit is the only sanctioned home for it, but this script sources
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


def test_inherited_production_notifier_is_cleared() -> None:
    """#277: a dev server must not notify the production tenant.

    /etc/watcher/.env carries WATCHER_NOTIFIER_BASE_URL and WATCHER_NOTIFIER_API_KEY, and this
    script sources it. The dev server runs the embedded worker against a real
    check pipeline, so an inherited key means real deliveries to real
    subscribers — the #233 hazard, notifier edition, and the only one of the
    three whose stray output cannot be recalled.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_NOTIFIER_BASE_URL": "http://localhost:9000",
            "WATCHER_NOTIFIER_API_KEY": "nk_production",
        }
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER_NOTIFIER_BASE_URL=(cleared)" in result.stdout
    assert "WATCHER_NOTIFIER_ENABLED=(cleared)" in result.stdout


def test_explicit_dev_notifier_is_forwarded_and_opts_in() -> None:
    """A scratch notifier opts the sanctioned launch path in, as the bus does.

    The flag is otherwise unit-only, so without this the dev server would set a
    dev notifier URL and then refuse to start on the very gate that exists to
    stop *unsanctioned* processes.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_NOTIFIER_BASE_URL": "http://localhost:9000",
            "WATCHER_NOTIFIER_API_KEY": "nk_production",
            "WATCHER_DEV_NOTIFIER_BASE_URL": "http://localhost:9001",
            "WATCHER_DEV_NOTIFIER_API_KEY": "nk_dev",
        }
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER_NOTIFIER_BASE_URL=http://localhost:9001" in result.stdout
    assert "WATCHER_NOTIFIER_ENABLED=1" in result.stdout


def test_dev_notifier_url_without_a_dev_key_refuses() -> None:
    """Half a scratch notifier is a misconfiguration, not a mode.

    Falling back to the inherited WATCHER_NOTIFIER_API_KEY would point a dev base URL
    at production credentials — or, if the dev notifier ignores the key,
    silently authorise the wrong tenant. Neither is worth guessing at, and the
    pair is two lines in .env.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_NOTIFIER_API_KEY": "nk_production",
            "WATCHER_DEV_NOTIFIER_BASE_URL": "http://localhost:9001",
        }
    )
    assert result.returncode != 0
    assert "WATCHER_DEV_NOTIFIER_API_KEY" in result.stderr


def test_an_inherited_notifier_opt_in_does_not_survive() -> None:
    """A flag leaked into an env file must not re-arm the production tenant.

    The unit is the only sanctioned home for it, but this script sources
    /etc/watcher/.env and the repo .env — so it clears what it did not set.
    """
    result = run(
        {
            "TEST_DATABASE_URL": TEST_URL,
            "WATCHER_NOTIFIER_BASE_URL": "http://localhost:9000",
            "WATCHER_NOTIFIER_API_KEY": "nk_production",
            "WATCHER_NOTIFIER_ENABLED": "1",
        }
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER_NOTIFIER_BASE_URL=(cleared)" in result.stdout
    assert "WATCHER_NOTIFIER_ENABLED=(cleared)" in result.stdout
