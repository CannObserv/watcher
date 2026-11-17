"""The #414 migration: a nullable, CHECK-constrained ``render`` on two tables.

Runs the migration's own ``upgrade()`` / ``downgrade()`` against the test
database through an Alembic ``Operations`` context, inside the ``db_session``
transaction, so the DDL rolls back with the test. The test schema is built from
the models, which already map the column, so each test starts upgraded.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from src.core.models.watched_item import RENDER_MODES
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_TABLES = ("watched_items", "fetch_commands")


def _migration():
    (path,) = _VERSIONS.glob("*_add_render_mode_414.py")
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


async def _schema(db_session) -> dict:
    def _read(sync_conn):
        inspector = inspect(sync_conn)
        return {
            table: {
                "columns": {c["name"]: c for c in inspector.get_columns(table)},
                "checks": {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table)},
            }
            for table in _TABLES
        }

    conn = await db_session.connection()
    return await conn.run_sync(_read)


def test_it_follows_the_head_it_was_written_on():
    assert _migration().down_revision == "c8e1d4f7a2b6"


def test_the_migration_allows_exactly_cocores_modes():
    """The migration spells the modes out (a migration is frozen); co-core's
    ``RenderMode`` is what the models read. A co-core bump that moves the
    vocabulary fails here, and needs a migration of its own."""
    assert tuple(_migration().RENDER_MODES) == RENDER_MODES


@pytest.mark.parametrize("table", _TABLES)
async def test_upgrade_adds_a_nullable_checked_column(db_session, table):
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")

    schema = (await _schema(db_session))[table]
    assert schema["columns"]["render"]["nullable"] is True
    check = schema["checks"][f"ck_{table}_render"]
    for mode in RENDER_MODES:
        assert f"'{mode}'" in check


@pytest.mark.parametrize("table", _TABLES)
async def test_downgrade_drops_the_column_and_its_check(db_session, table):
    await _run(db_session, "downgrade")

    schema = (await _schema(db_session))[table]
    assert "render" not in schema["columns"]
    assert f"ck_{table}_render" not in schema["checks"]


@pytest.mark.parametrize("value", [None, *RENDER_MODES])
async def test_the_check_admits_null_and_every_mode(db_session, value):
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")
    wi = await make_watched_item(db_session)
    await db_session.flush()

    await db_session.execute(
        text("UPDATE watched_items SET render = :v WHERE id = :id"), {"v": value, "id": str(wi.id)}
    )


@pytest.mark.parametrize("value", ["HTTP", "chrome", ""])
async def test_the_check_refuses_an_unsendable_mode(db_session, value):
    # Replicator matches the mode exactly, so "HTTP" is as unsendable as any.
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")
    wi = await make_watched_item(db_session)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="ck_watched_items_render"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE watched_items SET render = :v WHERE id = :id"),
                {"v": value, "id": str(wi.id)},
            )
