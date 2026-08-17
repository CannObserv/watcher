"""Alembic migration environment — async PostgreSQL."""

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from src.core.database import get_migration_database_url
from src.core.models import Base
from src.core.models.base import ULIDType

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: alembic also runs in-process (the
    # migration-chain test, dev tooling), and the stdlib default of True would
    # silently disable every already-created application logger for the rest of
    # the process — records vanish without an error.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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
    """Return the URL to migrate: the migration role's, else the app's.

    Alembic is the one thing that needs DDL rights, so since #259 it reads
    ``WATCHER_MIGRATION_DATABASE_URL`` first and falls back to ``DATABASE_URL``
    — which is every host that has not run ``scripts/setup-db-roles.sql`` yet.
    Resolution (and the divergence warning) lives in ``src.core.database`` so
    it is testable without importing this module, whose import runs migrations.
    """
    return get_migration_database_url(config.get_main_option("sqlalchemy.url", ""))


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
