"""Tests for scripts/migrate_watches_to_v2.py."""

import pytest

from scripts.migrate_watches_to_v2 import MissingMappingError, migrate_watches
from tests.conftest import make_info_item, make_info_source, make_watch

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_happy_path_assigns_info_source_id(db_session):
    item = await make_info_item(db_session, name="X")
    source = await make_info_source(db_session)
    watch = await make_watch(db_session, info_item_id=item.info_item_id)
    await db_session.flush()
    assert watch.info_source_id is None

    manifest = {str(item.info_item_id): str(source.info_source_id)}
    await migrate_watches(db_session, manifest)
    await db_session.refresh(watch)
    assert str(watch.info_source_id) == str(source.info_source_id)


@pytest.mark.asyncio
async def test_hard_errors_on_missing_mapping(db_session):
    item = await make_info_item(db_session, name="Orphan")
    watch = await make_watch(db_session, info_item_id=item.info_item_id)
    await db_session.flush()
    with pytest.raises(MissingMappingError) as exc:
        await migrate_watches(db_session, manifest={})
    assert str(item.info_item_id) in str(exc.value)
    assert str(watch.id) in str(exc.value)


@pytest.mark.asyncio
async def test_idempotent_re_run(db_session):
    """Re-running over already-migrated Watches is a no-op."""
    item = await make_info_item(db_session, name="Y")
    source = await make_info_source(db_session)
    watch = await make_watch(
        db_session,
        info_item_id=item.info_item_id,
        info_source_id=source.info_source_id,
    )
    await migrate_watches(db_session, manifest={})  # empty, no NULL rows to fix
    await db_session.refresh(watch)
    assert str(watch.info_source_id) == str(source.info_source_id)
