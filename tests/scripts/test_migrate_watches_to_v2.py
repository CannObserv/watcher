"""Tests for scripts/migrate_watches_to_v2.py.

As of Task 5.5, Watch.info_item_id is dropped and info_source_id is NOT NULL.
The migrate_watches function is now a no-op guard: it raises MissingMappingError
if any Watch has NULL info_source_id (which should never happen post-migration),
and succeeds silently if all rows are already populated.
"""

import pytest

from scripts.migrate_watches_to_v2 import migrate_watches
from tests.conftest import make_info_source, make_watch

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_happy_path_noop_when_all_migrated(db_session):
    """migrate_watches is a no-op when all Watches already have info_source_id."""
    await make_watch(db_session)
    await db_session.flush()
    # Should not raise
    await migrate_watches(db_session, manifest={})


@pytest.mark.asyncio
async def test_idempotent_re_run(db_session):
    """Re-running over already-migrated Watches is a no-op."""
    source = await make_info_source(db_session)
    watch = await make_watch(
        db_session,
        info_source_id=source.info_source_id,
    )
    await migrate_watches(db_session, manifest={})
    await db_session.refresh(watch)
    assert str(watch.info_source_id) == str(source.info_source_id)
