"""The #376 drop: ``watched_items.last_error_notified_at`` goes with #71's reminder.

Runs the migration's own ``upgrade()`` / ``downgrade()`` against the test
database through an Alembic ``Operations`` context, inside the ``db_session``
transaction, so the DDL rolls back with the test (Postgres DDL is
transactional). The test schema is built from the models, which no longer map
the column, so each test starts without it.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_TABLE = "watched_items"
_COLUMN = "last_error_notified_at"


def _migration():
    (path,) = _VERSIONS.glob("*_drop_last_error_notified_at_376.py")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(db_session, step: str) -> None:
    def _apply(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(_migration(), step)()

    conn = await db_session.connection()
    await conn.run_sync(_apply)


async def _column(db_session) -> dict | None:
    def _read(sync_conn):
        columns = {c["name"]: c for c in inspect(sync_conn).get_columns(_TABLE)}
        return columns.get(_COLUMN)

    conn = await db_session.connection()
    return await conn.run_sync(_read)


def test_it_follows_the_head_it_was_written_on():
    assert _migration().down_revision == "7f3d3491e921"


async def test_the_models_no_longer_map_the_column(db_session):
    assert await _column(db_session) is None


async def test_downgrade_restores_the_36358e2f5ad4_shape(db_session):
    """Nullable, timezone-aware, no default; the values are not restored."""
    await _run(db_session, "downgrade")

    column = await _column(db_session)
    assert column is not None
    assert column["nullable"] is True
    assert column["type"].timezone is True
    assert column["default"] is None


async def test_upgrade_drops_the_column(db_session):
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")
    assert await _column(db_session) is None


async def test_upgrade_is_idempotent(db_session):
    """``IF EXISTS``: a database an operator already hand-pruned still upgrades."""
    await _run(db_session, "upgrade")
    await _run(db_session, "upgrade")
    assert await _column(db_session) is None
