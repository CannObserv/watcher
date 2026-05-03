"""Alembic environment for the Information service.

Tables are scoped to a Postgres `information` schema; this env filters
autogenerate so it only sees objects in that schema (and ignores Watcher's
tables in `public`).
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from src.information.core.models import Base
from src.information.core.models.base import ULIDType

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

INFORMATION_SCHEMA = "information"


def include_object(object_, name, type_, reflected, compare_to):
    """Restrict autogenerate to objects in the `information` schema.

    Without this, alembic would compare Information's metadata against the
    full live database (including Watcher's `public`-schema tables) and emit
    spurious drop_table operations.
    """
    if type_ == "table":
        return getattr(object_, "schema", None) == INFORMATION_SCHEMA
    if type_ in ("index", "unique_constraint", "foreign_key_constraint"):
        table = getattr(object_, "table", None)
        if table is None:
            return True
        return getattr(table, "schema", None) == INFORMATION_SCHEMA
    return True


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, ULIDType):
        return "sa.String(length=26)"
    return False


def get_url() -> str:
    url = (
        os.environ.get("INFORMATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url", "")
    )
    if not url:
        raise RuntimeError(
            "Set INFORMATION_DATABASE_URL or DATABASE_URL before running alembic. "
            "Load env: export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)"
        )
    return url


def _common_configure_kwargs() -> dict:
    return dict(
        target_metadata=target_metadata,
        include_object=include_object,
        include_schemas=True,
        version_table_schema=INFORMATION_SCHEMA,
        render_item=render_item,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_common_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, **_common_configure_kwargs())
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(get_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
