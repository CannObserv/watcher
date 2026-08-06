"""Tests for ``src.core.db_safety`` — the production-database startup guard.

Background (#233, ported from archiver#98/#99): archiver's documented
dev-server recipe sourced ``/etc/archiver/.env`` and ran uvicorn directly,
leaving its database URL pointed at production; a dashboard verification run
wrote test rows into the production registry. Watcher's AGENTS.md and
docs/COMMANDS.md carried the identical recipe (``export $(cat
/etc/watcher/.env …)`` + uvicorn on 8001), so a dev server here would have
served the production ``watcher`` database — and, worse, started a second
embedded Procrastinate worker against the production queue and a second
``content.blobs`` consumer stealing production fact deliveries.

``scripts/dev_server.sh`` fixes the sanctioned launch path, but a docs-side fix
has the same failure mode as the docs bug it patches — a hand-rolled uvicorn,
or a stale recipe copied from an old plan doc, still reaches production. This
module is the launch-path-independent backstop: the application itself refuses
to serve a production database unless the caller opts in explicitly, which only
the systemd unit does.
"""

import pytest

from src.core.db_safety import (
    ProductionDatabaseRefused,
    assert_environment_db_allowed,
    assert_production_db_allowed,
    database_name,
    is_non_production_database,
)

PROD = "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher"
TEST = "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher_test"
DEV = "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher_dev"


class TestDatabaseName:
    """URL → database name extraction."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (PROD, "watcher"),
            (TEST, "watcher_test"),
            ("postgresql://watcher:watcher@localhost:5432/watcher", "watcher"),
            ("postgresql+psycopg://u:p@host:5432/watcher_test", "watcher_test"),
            # No port.
            ("postgresql://u:p@host/watcher", "watcher"),
            # Query string must not bleed into the name.
            ("postgresql://u:p@host:5432/watcher_test?sslmode=require", "watcher_test"),
            # Password containing a slash must not be mistaken for a path.
            ("postgresql://u:p%2Fw@host:5432/watcher", "watcher"),
        ],
    )
    def test_extracts_name(self, url: str, expected: str) -> None:
        assert database_name(url) == expected

    @pytest.mark.parametrize(
        "url", ["", "not-a-url", "postgresql://host:5432/", "postgresql://host"]
    )
    def test_returns_none_when_undeterminable(self, url: str) -> None:
        """An unparseable URL yields None so callers can fail closed."""
        assert database_name(url) is None


class TestIsNonProductionDatabase:
    """Positive assertion — the name must *look* disposable.

    Comparing the dev URL to the prod URL by string equality is defeated by a
    cosmetic difference (``postgresql://`` vs ``postgresql+asyncpg://``,
    ``localhost`` vs ``127.0.0.1``). Asserting the database *name* carries a
    ``_test``/``_dev`` suffix is not bypassable that way, and it matches the
    convention AGENTS.md already documents for TEST_DATABASE_URL.
    """

    @pytest.mark.parametrize(
        "url", [TEST, DEV, "postgresql://h/anything_test", "postgresql://h/x_dev"]
    )
    def test_accepts_test_and_dev_suffixes(self, url: str) -> None:
        assert is_non_production_database(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            PROD,
            "postgresql://h/watcher",
            # Substring, not suffix — must not pass.
            "postgresql://h/test_watcher",
            "postgresql://h/watcher_testing",
            "postgresql://h/devious",
        ],
    )
    def test_rejects_production_looking_names(self, url: str) -> None:
        assert is_non_production_database(url) is False

    def test_unparseable_url_is_treated_as_production(self) -> None:
        """Fail closed: if we cannot read the name, assume it is production."""
        assert is_non_production_database("not-a-url") is False


class TestAssertProductionDbAllowed:
    """The startup gate itself."""

    def test_allows_non_production_database_without_opt_in(self) -> None:
        assert_production_db_allowed(TEST, allow_flag=None)

    def test_refuses_production_database_without_opt_in(self) -> None:
        """The incident condition, from any launch path."""
        with pytest.raises(ProductionDatabaseRefused) as excinfo:
            assert_production_db_allowed(PROD, allow_flag=None)
        message = str(excinfo.value)
        assert "watcher" in message
        assert "WATCHER_ALLOW_PRODUCTION_DB" in message

    def test_allows_production_database_with_explicit_opt_in(self) -> None:
        """Only the systemd unit sets this; it is how the live service starts."""
        assert_production_db_allowed(PROD, allow_flag="1")

    @pytest.mark.parametrize("flag", ["0", "", "true", "yes", "no"])
    def test_only_exact_1_opts_in(self, flag: str) -> None:
        """A fuzzy truthiness check would let a stray value re-open the hole."""
        with pytest.raises(ProductionDatabaseRefused):
            assert_production_db_allowed(PROD, allow_flag=flag)

    def test_refuses_unparseable_url_without_opt_in(self) -> None:
        with pytest.raises(ProductionDatabaseRefused):
            assert_production_db_allowed("not-a-url", allow_flag=None)


class TestAssertEnvironmentDbAllowed:
    """The env-facing wrapper the lifespan calls.

    Watcher has two ways into a database: ``DATABASE_URL`` (API + fallback for
    the embedded worker) and ``PROCRASTINATE_DATABASE_URL`` (worker override —
    ``src/workers/__init__.py`` consults it first). Both must pass, or a dev
    process with a test DATABASE_URL could still run its worker against the
    production queue.
    """

    def test_allows_when_both_urls_are_non_production(self) -> None:
        assert_environment_db_allowed({"DATABASE_URL": TEST, "PROCRASTINATE_DATABASE_URL": DEV})

    def test_allows_test_database_url_with_no_worker_override(self) -> None:
        assert_environment_db_allowed({"DATABASE_URL": TEST})

    def test_refuses_production_database_url(self) -> None:
        with pytest.raises(ProductionDatabaseRefused):
            assert_environment_db_allowed({"DATABASE_URL": PROD})

    def test_refuses_production_worker_url_even_with_test_database_url(self) -> None:
        """A prod PROCRASTINATE_DATABASE_URL is a second worker on the prod queue."""
        with pytest.raises(ProductionDatabaseRefused):
            assert_environment_db_allowed(
                {"DATABASE_URL": TEST, "PROCRASTINATE_DATABASE_URL": PROD}
            )

    def test_refuses_missing_database_url(self) -> None:
        """No URL at all fails closed (startup would fail later anyway)."""
        with pytest.raises(ProductionDatabaseRefused):
            assert_environment_db_allowed({})

    def test_opt_in_covers_both_urls(self) -> None:
        assert_environment_db_allowed(
            {
                "DATABASE_URL": PROD,
                "PROCRASTINATE_DATABASE_URL": PROD,
                "WATCHER_ALLOW_PRODUCTION_DB": "1",
            }
        )
