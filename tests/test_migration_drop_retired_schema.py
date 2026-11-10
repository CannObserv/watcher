"""The #380 drop: the headless jump's retired tables and columns.

Runs the migration's own ``upgrade()`` / ``downgrade()`` against the test
database through an Alembic ``Operations`` context, inside the ``db_session``
transaction, so the DDL rolls back with the test (Postgres DDL is
transactional). The test schema is built from the models, which no longer map
any of it, so each test starts without it.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_TABLES = ("notification_templates", "temporal_profiles", "api_keys", "app_users")
_COLUMNS = {
    "watched_items": (
        "name",
        "description",
        "default_tags",
        "default_schedule_config",
        "domain_default_schedule_config",
        "throttle_floor_interval",
        "last_reviewed_at",
        "archived_at",
    ),
    "domains": ("min_interval", "default_schedule_config", "notes", "archived_at"),
}


def _migration():
    (path,) = _VERSIONS.glob("*_drop_retired_schema_380.py")
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
    """``{"tables": set, "columns": {table: {name: column}}, "fks": {table: [...]}}``."""

    def _read(sync_conn):
        inspector = inspect(sync_conn)
        tables = set(inspector.get_table_names())
        return {
            "tables": tables,
            "columns": {
                table: {c["name"]: c for c in inspector.get_columns(table)}
                for table in (*_COLUMNS, *(t for t in _TABLES if t in tables))
            },
            "fks": {
                table: inspector.get_foreign_keys(table) for table in _TABLES if table in tables
            },
            "uniques": {
                table: inspector.get_unique_constraints(table)
                for table in _TABLES
                if table in tables
            },
            "checks": {
                table: inspector.get_check_constraints(table)
                for table in _TABLES
                if table in tables
            },
        }

    conn = await db_session.connection()
    return await conn.run_sync(_read)


def _assert_dropped(schema: dict) -> None:
    assert not set(_TABLES) & schema["tables"]
    for table, columns in _COLUMNS.items():
        assert not set(columns) & set(schema["columns"][table]), table


def test_it_follows_the_head_it_was_written_on():
    assert _migration().down_revision == "e2eab993d615"


async def test_the_models_no_longer_map_any_of_it(db_session):
    _assert_dropped(await _schema(db_session))


async def test_downgrade_restores_every_table_and_column(db_session):
    await _run(db_session, "downgrade")

    schema = await _schema(db_session)
    assert set(_TABLES) <= schema["tables"]
    for table, columns in _COLUMNS.items():
        assert set(columns) <= set(schema["columns"][table]), table


async def test_downgrade_restores_name_nullable(db_session):
    """Genesis made ``name`` NOT NULL with no default; re-adding that to a table
    with rows fails, so the downgrade restores it nullable and empty."""
    await _run(db_session, "downgrade")

    name = (await _schema(db_session))["columns"]["watched_items"]["name"]
    assert name["nullable"] is True
    assert name["type"].length == 255


async def test_downgrade_restores_min_interval_with_its_server_default(db_session):
    """NOT NULL is safe here: the server default fills existing rows."""
    await _run(db_session, "downgrade")

    column = (await _schema(db_session))["columns"]["domains"]["min_interval"]
    assert column["nullable"] is False
    assert column["default"] == "'1'::double precision"  # genesis's server_default="1.0"


async def test_downgrade_restores_the_genesis_constraints(db_session):
    await _run(db_session, "downgrade")
    schema = await _schema(db_session)

    def referred(table):
        return sorted(
            (fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete"))
            for fk in schema["fks"][table]
        )

    assert referred("api_keys") == [("user_id", "app_users", None)]
    assert referred("temporal_profiles") == [("watched_item_id", "watched_items", "CASCADE")]
    assert referred("notification_templates") == [
        ("domain_name", "domains", "CASCADE"),
        ("watched_item_id", "watched_items", "CASCADE"),
    ]
    assert [u["column_names"] for u in schema["uniques"]["api_keys"]] == [["key_hash"]]
    assert [u["column_names"] for u in schema["uniques"]["temporal_profiles"]] == [
        ["watched_item_id"]
    ]
    assert [c["name"] for c in schema["checks"]["notification_templates"]] == [
        "ck_notification_templates_visibility_refs"
    ]


async def test_upgrade_drops_it_all(db_session):
    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")
    _assert_dropped(await _schema(db_session))


async def test_upgrade_is_idempotent(db_session):
    """``IF EXISTS``: a database an operator already hand-pruned still upgrades."""
    await _run(db_session, "upgrade")
    await _run(db_session, "upgrade")
    _assert_dropped(await _schema(db_session))
