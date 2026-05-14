"""Effective cadence = min(root.schedule, min(fragment_schedules))."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.watches.cadence import effective_root_cadence_seconds
from tests.conftest import make_info_source, make_watch

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_returns_root_when_no_fragments(db_session):
    root_src = await make_info_source(db_session)
    root = await make_watch(
        db_session,
        info_source_id=root_src.info_source_id,
        schedule_config={"interval_seconds": 3600},
    )
    client = MagicMock()
    client.list_info_sources = AsyncMock(return_value=MagicMock(items=[]))
    assert await effective_root_cadence_seconds(db_session, client, root) == 3600


@pytest.mark.asyncio
async def test_min_of_root_and_fragment_schedules(db_session):
    root_src = await make_info_source(db_session)
    frag1_src = await make_info_source(db_session, parent_info_source_id=root_src.info_source_id)
    frag2_src = await make_info_source(db_session, parent_info_source_id=root_src.info_source_id)
    root = await make_watch(
        db_session,
        info_source_id=root_src.info_source_id,
        schedule_config={"interval_seconds": 3600},
    )
    await make_watch(
        db_session,
        info_source_id=frag1_src.info_source_id,
        schedule_config={"interval_seconds": 900},
    )
    await make_watch(
        db_session,
        info_source_id=frag2_src.info_source_id,
        schedule_config={"interval_seconds": 600},
    )
    client = MagicMock()
    client.list_info_sources = AsyncMock(
        return_value=MagicMock(
            items=[
                MagicMock(info_source_id=frag1_src.info_source_id),
                MagicMock(info_source_id=frag2_src.info_source_id),
            ]
        )
    )
    seconds = await effective_root_cadence_seconds(db_session, client, root)
    assert seconds == 600


@pytest.mark.asyncio
async def test_inactive_fragment_watch_excluded(db_session):
    root_src = await make_info_source(db_session)
    frag_src = await make_info_source(db_session, parent_info_source_id=root_src.info_source_id)
    root = await make_watch(
        db_session,
        info_source_id=root_src.info_source_id,
        schedule_config={"interval_seconds": 3600},
    )
    frag_watch = await make_watch(
        db_session,
        info_source_id=frag_src.info_source_id,
        schedule_config={"interval_seconds": 600},
    )
    # Archive after creation so the watch is excluded
    frag_watch.is_archived = True
    await db_session.flush()

    client = MagicMock()
    client.list_info_sources = AsyncMock(
        return_value=MagicMock(
            items=[
                MagicMock(info_source_id=frag_src.info_source_id),
            ]
        )
    )
    assert await effective_root_cadence_seconds(db_session, client, root) == 3600
