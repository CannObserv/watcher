"""The #71 migration's backfill: items already in ERROR start the clock.

Without it every item that was in ERROR at deploy would re-notify on its next
failed check — ``last_error_notified_at IS NULL`` is "due" — a burst nobody
asked for. Runs the migration's own backfill against the test database through
an Alembic ``Operations`` context, inside the ``db_session`` transaction (the
column itself already exists there: pytest builds the schema with
``create_all``).
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _migration():
    (path,) = _VERSIONS.glob("*_last_error_notified_at_71.py")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _backfill(db_session) -> None:
    def _apply(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            _migration().backfill_error_items()

    conn = await db_session.connection()
    await conn.run_sync(_apply)


async def _stamp(db_session, wi) -> datetime | None:
    query = select(WatchedItem.last_error_notified_at).where(WatchedItem.id == wi.id)
    return (await db_session.execute(query)).scalar_one()


async def test_error_items_are_stamped_and_others_are_not(db_session):
    erroring = await make_watched_item(db_session, primary_url="https://a.example/x")
    erroring.health_status = WatchHealthStatus.ERROR
    healthy = await make_watched_item(db_session, primary_url="https://b.example/x")
    healthy.health_status = WatchHealthStatus.OK
    unknown = await make_watched_item(db_session, primary_url="https://c.example/x")
    await db_session.flush()

    await _backfill(db_session)

    stamp = await _stamp(db_session, erroring)
    assert stamp is not None
    assert abs(stamp - datetime.now(UTC)) < timedelta(minutes=5)
    assert await _stamp(db_session, healthy) is None
    assert await _stamp(db_session, unknown) is None


async def test_an_existing_stamp_is_kept(db_session):
    wi = await make_watched_item(db_session, primary_url="https://a.example/x")
    wi.health_status = WatchHealthStatus.ERROR
    told = datetime(2026, 10, 1, tzinfo=UTC)
    wi.last_error_notified_at = told
    await db_session.flush()

    await _backfill(db_session)

    assert await _stamp(db_session, wi) == told
