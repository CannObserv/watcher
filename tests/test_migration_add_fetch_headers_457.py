"""The #457 migration: two nullable JSONB policy maps on two tables.

Runs the migration's own ``upgrade()`` / ``downgrade()`` against the test
database through an Alembic ``Operations`` context, inside the ``db_session``
transaction, so the DDL rolls back with the test. The test schema is built from
the models, which already map the columns, so each test starts upgraded.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB

from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_TABLES = ("watched_items", "fetch_commands")
_COLUMNS = ("fetch_headers", "fetch_credential_headers")


def _migration():
    (path,) = _VERSIONS.glob("*_add_fetch_header_policy_457.py")
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


async def _columns(db_session, table: str) -> dict:
    def _read(sync_conn):
        return {c["name"]: c for c in inspect(sync_conn).get_columns(table)}

    conn = await db_session.connection()
    return await conn.run_sync(_read)


def test_it_follows_the_head_it_was_written_on():
    assert _migration().down_revision == "6b41fb81304a"


@pytest.mark.parametrize("table", _TABLES)
async def test_upgrade_adds_two_nullable_jsonb_columns(db_session, table):
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")

    columns = await _columns(db_session, table)
    for name in _COLUMNS:
        assert columns[name]["nullable"] is True
        assert isinstance(columns[name]["type"], JSONB)
        assert columns[name]["default"] is None


@pytest.mark.parametrize("table", _TABLES)
async def test_downgrade_drops_both_columns(db_session, table):
    await _run(db_session, "downgrade")

    columns = await _columns(db_session, table)
    for name in _COLUMNS:
        assert name not in columns


async def test_an_existing_row_reads_null_after_upgrade(db_session):
    """No back-fill: NULL is no policy, today's frame."""
    wi = await make_watched_item(db_session)
    await db_session.flush()
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")

    row = (
        await db_session.execute(
            text(
                "SELECT fetch_headers IS NULL, fetch_credential_headers IS NULL "
                "FROM watched_items WHERE id = :id"
            ),
            {"id": str(wi.id)},
        )
    ).one()
    assert tuple(row) == (True, True)
