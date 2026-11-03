"""The #374 drop: ``fetch_policy_tombstones`` goes with Watcher's producer.

Runs the migration's own ``upgrade()`` / ``downgrade()`` against the test
database through an Alembic ``Operations`` context, inside the ``db_session``
transaction, so the DDL rolls back with the test (Postgres DDL is
transactional). The test schema is built from the models, which no longer map
the table, so each test starts without it.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_TABLE = "fetch_policy_tombstones"


def _migration():
    (path,) = _VERSIONS.glob("*_drop_fetch_policy_tombstones_374.py")
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


async def _columns(db_session) -> dict[str, dict] | None:
    def _read(sync_conn):
        inspector = inspect(sync_conn)
        if not inspector.has_table(_TABLE):
            return None
        pk = inspector.get_pk_constraint(_TABLE)["constrained_columns"]
        return {
            c["name"]: {**c, "primary_key": c["name"] in pk} for c in inspector.get_columns(_TABLE)
        }

    conn = await db_session.connection()
    return await conn.run_sync(_read)


def test_it_follows_the_head_it_was_written_on():
    assert _migration().down_revision == "5580ec57b7aa"


async def test_the_models_no_longer_map_the_table(db_session):
    assert await _columns(db_session) is None


async def test_downgrade_recreates_the_c398e0917f16_shape(db_session):
    await _run(db_session, "downgrade")

    columns = await _columns(db_session)
    assert columns is not None
    assert set(columns) == {"host", "revoked_at", "created_at", "updated_at"}
    assert columns["host"]["primary_key"] is True
    assert columns["host"]["type"].length == 253
    assert all(not c["nullable"] for c in columns.values())
    assert "now()" in str(columns["created_at"]["default"])
    assert "now()" in str(columns["updated_at"]["default"])
    assert columns["revoked_at"]["default"] is None


async def test_upgrade_drops_the_table(db_session):
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")
    assert await _columns(db_session) is None


async def test_upgrade_is_idempotent(db_session):
    """``IF EXISTS``: a database an operator already hand-pruned still upgrades."""
    await _run(db_session, "upgrade")
    await _run(db_session, "upgrade")
    assert await _columns(db_session) is None
