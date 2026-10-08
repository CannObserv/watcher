"""The #166 data migration: strip the five never-firing events from templates.

Runs the migration's own ``upgrade()`` / ``downgrade()`` against the test
database through an Alembic ``Operations`` context, inside the ``db_session``
transaction, so the rows it rewrites roll back with the test.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from ulid import ULID

from src.core.models.notification_template import NotificationTemplate

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_DROPPED = ["watch_created", "watch_paused", "watch_resumed", "watch_archived", "watch_deleted"]
_KEPT = ["change_detected", "watch_error", "watch_recovered"]
_OPTS = {"include_tags": True}


def _migration():
    (path,) = _VERSIONS.glob("*_strip_never_firing_events_166.py")
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


async def _template(db_session, events, content_config=None) -> NotificationTemplate:
    tpl = NotificationTemplate(
        title=f"T {ULID()}",
        remote_channel_id=str(ULID()),
        channel_hint="json",
        events=events,
        content_config=content_config,
    )
    db_session.add(tpl)
    await db_session.flush()
    return tpl


async def _reload(db_session, tpl) -> NotificationTemplate:
    tpl_id = tpl.id  # before expiry: reading it after would lazy-load outside a greenlet
    db_session.expire_all()
    query = select(NotificationTemplate).where(NotificationTemplate.id == tpl_id)
    return (await db_session.execute(query)).scalar_one()


async def test_strips_every_dropped_event_and_keeps_the_rest_in_order(db_session):
    """The email template's shape on production: all four lifecycle events."""
    tpl = await _template(db_session, ["change_detected", *_DROPPED[1:], "watch_error"])
    await _run(db_session, "upgrade")
    assert (await _reload(db_session, tpl)).events == ["change_detected", "watch_error"]


async def test_strips_watch_created(db_session):
    """Neither live row holds it; a dev or restored database may."""
    tpl = await _template(db_session, ["watch_created", "watch_recovered"])
    await _run(db_session, "upgrade")
    assert (await _reload(db_session, tpl)).events == ["watch_recovered"]


async def test_leaves_a_row_with_only_live_events_alone(db_session):
    tpl = await _template(db_session, list(_KEPT))
    await _run(db_session, "upgrade")
    assert (await _reload(db_session, tpl)).events == _KEPT


async def test_a_row_with_only_dropped_events_is_left_subscribed_to_nothing(db_session):
    """It never fired, and it still does not. Substituting ``change_detected``
    would start delivering notifications nobody subscribed to."""
    tpl = await _template(db_session, ["watch_paused", "watch_resumed"])
    await _run(db_session, "upgrade")
    assert (await _reload(db_session, tpl)).events == []


async def test_strips_per_event_overrides_keyed_by_a_dropped_event(db_session):
    """``ContentConfig`` rejects an override key outside ``WatchEventType``, so
    one left behind would fail every read of the row through the schema."""
    config = {"default": {}, "overrides": {"watch_paused": _OPTS, "change_detected": _OPTS}}
    tpl = await _template(db_session, ["change_detected", "watch_paused"], config)
    await _run(db_session, "upgrade")
    reloaded = await _reload(db_session, tpl)
    assert reloaded.content_config == {"default": {}, "overrides": {"change_detected": _OPTS}}


async def test_leaves_a_null_content_config_null(db_session):
    tpl = await _template(db_session, ["change_detected", "watch_archived"])
    await _run(db_session, "upgrade")
    assert (await _reload(db_session, tpl)).content_config is None


async def test_upgrade_is_idempotent(db_session):
    tpl = await _template(db_session, ["change_detected", "watch_deleted"])
    await _run(db_session, "upgrade")
    await _run(db_session, "upgrade")
    assert (await _reload(db_session, tpl)).events == ["change_detected"]


async def test_downgrade_restores_nothing(db_session):
    """The stripped values never fired; restoring them would only re-offer a
    subscription that cannot deliver."""
    tpl = await _template(db_session, ["change_detected", "watch_paused"])
    await _run(db_session, "upgrade")
    await _run(db_session, "downgrade")
    assert (await _reload(db_session, tpl)).events == ["change_detected"]
