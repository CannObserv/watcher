"""The test session must never be able to reach the production database.

Pins the conftest import-time guard, the sibling of ``test_bus_isolation``.
``tests/conftest.py`` overwrites every environment variable that can name a
database with ``TEST_DATABASE_URL``, because anything resolving a URL from the
environment rather than through the ``get_db_session`` override — the engine
factory, Alembic's ``env.py``, the Procrastinate connector — would otherwise
read the *production* ``DATABASE_URL`` that ``/etc/watcher/.env`` supplies.

``WATCHER_MIGRATION_DATABASE_URL`` (#259) is the newest member of that set and
the most dangerous one, because it is the credential that holds DDL rights: an
unpinned value would let a suite that invokes Alembic migrate production while
``DATABASE_URL`` innocently pointed at the test database. The suite genuinely
needs DDL — ``test_engine`` does ``create_all``/``drop_all`` and Archiver's
migrations run as a subprocess — so the rule is *DDL on ``_test`` databases
only*, and this is what enforces the second half.
"""

import os

from src.core.database import (
    MIGRATION_DATABASE_URL_ENV,
    get_database_url,
    get_migration_database_url,
)
from tests.conftest import TEST_DATABASE_URL


class TestDatabaseIsolation:
    def test_application_url_is_the_test_database(self) -> None:
        assert os.environ["DATABASE_URL"] == TEST_DATABASE_URL
        assert get_database_url() == TEST_DATABASE_URL

    def test_migration_url_is_the_test_database(self) -> None:
        """Pinned, not cleared: cleared would fall back to DATABASE_URL, which
        is correct today but stops being correct the moment a test sets it."""
        assert os.environ[MIGRATION_DATABASE_URL_ENV] == TEST_DATABASE_URL
        assert get_migration_database_url() == TEST_DATABASE_URL

    def test_worker_url_override_is_cleared(self) -> None:
        """``src.workers`` consults it before ``DATABASE_URL`` (#233)."""
        assert os.environ.get("PROCRASTINATE_DATABASE_URL") is None
