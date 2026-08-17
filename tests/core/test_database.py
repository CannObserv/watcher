"""URL resolution for the application engine and for Alembic (#259).

Two roles, two URLs: the long-running service connects with the DML-only
application role (``DATABASE_URL``), Alembic connects with the schema owner
(``WATCHER_MIGRATION_DATABASE_URL``). The fallback is the whole point of the
resolver — on a single-role database, and on any host where the operator step
has not run yet, the migration URL is simply the application's.
"""

import logging

import pytest

from src.core.database import (
    MIGRATION_DATABASE_URL_ENV,
    get_database_url,
    get_migration_database_url,
)

_APP_URL = "postgresql+asyncpg://watcher_app:pw@localhost:5432/watcher"
_MIGRATE_URL = "postgresql+asyncpg://watcher:pw@localhost:5432/watcher"
_SCRATCH_URL = "postgresql+asyncpg://watcher:pw@localhost:5432/watcher_scratch"
_INI_URL = "postgresql+asyncpg://ini:ini@localhost:5432/from_ini"


@pytest.fixture(autouse=True)
def _clear_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every case from an environment that names no database.

    ``tests/conftest.py`` pins both variables for the session, so without this
    each case would read the test database rather than what it set.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(MIGRATION_DATABASE_URL_ENV, raising=False)


class TestApplicationUrl:
    def test_reads_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        assert get_database_url() == _APP_URL

    def test_ignores_the_migration_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The service must never pick up the schema owner's credential."""
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        monkeypatch.setenv(MIGRATION_DATABASE_URL_ENV, _MIGRATE_URL)
        assert get_database_url() == _APP_URL


class TestMigrationUrl:
    def test_prefers_the_migration_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        monkeypatch.setenv(MIGRATION_DATABASE_URL_ENV, _MIGRATE_URL)
        assert get_migration_database_url() == _MIGRATE_URL

    def test_falls_back_to_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pre-#259 behaviour, and the behaviour on every host until the
        operator step runs: one role, one URL, Alembic uses the app's."""
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        assert get_migration_database_url() == _APP_URL

    def test_empty_migration_url_is_not_a_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty value falls back rather than resolving to ``""``.

        ``EnvironmentFile`` lines and ``KEY=`` in a shell export both produce a
        set-but-empty variable; treating that as a URL would hand Alembic an
        unconnectable engine instead of the fallback.
        """
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        monkeypatch.setenv(MIGRATION_DATABASE_URL_ENV, "")
        assert get_migration_database_url() == _APP_URL

    def test_falls_back_to_the_supplied_default(self) -> None:
        """Neither variable set — the caller's default (alembic.ini) wins."""
        assert get_migration_database_url(_INI_URL) == _INI_URL

    def test_no_url_anywhere_raises(self) -> None:
        """Fail loudly rather than hand back an empty URL.

        ``alembic.ini`` used to carry a real production URL as its default, so
        an unset ``DATABASE_URL`` silently migrated production. The default is
        now empty, which makes this the path a misconfigured shell takes.
        """
        with pytest.raises(RuntimeError, match=MIGRATION_DATABASE_URL_ENV):
            get_migration_database_url()


class TestDivergenceWarning:
    """Both URLs set and naming different databases is legitimate but loud.

    The scratch-database autogenerate workflow wants exactly that split, so it
    cannot be refused. It is also the shape of the mistake this change makes
    possible for the first time — migrating one database while the service
    serves another — so it is never silent.
    """

    def test_warns_when_the_two_name_different_databases(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        monkeypatch.setenv(MIGRATION_DATABASE_URL_ENV, _SCRATCH_URL)
        with caplog.at_level(logging.WARNING, logger="src.core.database"):
            assert get_migration_database_url() == _SCRATCH_URL
        assert "watcher_scratch" in caplog.text
        assert "watcher" in caplog.text

    def test_silent_when_both_name_the_same_database(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The production shape: two roles, one database, no warning."""
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        monkeypatch.setenv(MIGRATION_DATABASE_URL_ENV, _MIGRATE_URL)
        with caplog.at_level(logging.WARNING, logger="src.core.database"):
            get_migration_database_url()
        assert caplog.records == []

    def test_silent_on_the_fallback_path(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", _APP_URL)
        with caplog.at_level(logging.WARNING, logger="src.core.database"):
            get_migration_database_url()
        assert caplog.records == []
