"""Alembic migration environment — async PostgreSQL."""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from src.core.models import Base
from src.core.models.base import ULIDType

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render ULIDType as sa.String(26) in migrations."""
    if type_ == "type" and isinstance(obj, ULIDType):
        return "sa.String(length=26)"
    return False


def _include_object(object, name, type_, reflected, compare_to):
    """Restrict autogenerate to Watcher-owned tables in the public schema.

    Filters out:
    - Non-public schemas: the Information service owns ``information`` on a
      separate Alembic root (``alembic_information.ini``).
    - Procrastinate-managed tables: the task queue installs and migrates its
      own ``procrastinate_*`` tables at app startup; they should not appear
      in Watcher's Alembic diffs.
    """
    if hasattr(object, "schema") and object.schema not in (None, "public"):
        return False
    if type_ == "table" and name.startswith("procrastinate_"):
        return False
    if type_ == "index" and reflected and getattr(object, "table", None) is not None:
        if object.table.name.startswith("procrastinate_"):
            return False
    return True


def get_url() -> str:
    """Read database URL from environment or alembic.ini."""
    return os.environ.get(
        "DATABASE_URL",
        config.get_main_option("sqlalchemy.url", ""),
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without connecting."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        include_object=_include_object,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations using a sync connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        include_object=_include_object,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode — async connection."""
    connectable = create_async_engine(get_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
