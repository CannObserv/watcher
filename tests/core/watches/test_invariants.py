"""Invariants enforced at the Watch lifecycle layer."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.watches.invariants import (
    FragmentDependentsExistError,
    RootWatchMissingError,
    require_no_fragment_dependents,
    require_root_watch_on_chain,
)
from tests.conftest import make_info_item, make_info_source, make_watch

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_require_root_watch_passes_when_root_is_watched(db_session):
    root = await make_info_source(db_session)
    info_item = await make_info_item(db_session)
    await make_watch(
        db_session, info_item_id=info_item.info_item_id, info_source_id=root.info_source_id
    )
    frag_id = "01HZZ00000000000000FRAGMENT"
    client = MagicMock()
    client.get_info_source = AsyncMock(
        side_effect=[
            MagicMock(info_source_id=frag_id, parent_info_source_id=str(root.info_source_id)),
            MagicMock(info_source_id=str(root.info_source_id), parent_info_source_id=None),
        ]
    )
    # No exception expected.
    await require_root_watch_on_chain(db_session, client, info_source_id=frag_id)


@pytest.mark.asyncio
async def test_require_root_watch_rejects_orphan(db_session):
    """No Watch on chain → RootWatchMissingError."""
    frag_id = "01HZZ00000000000000FRAGMENT"
    root_id = "01HZZ00000000000000000ROOT1"
    client = MagicMock()
    client.get_info_source = AsyncMock(
        side_effect=[
            MagicMock(info_source_id=frag_id, parent_info_source_id=root_id),
            MagicMock(info_source_id=root_id, parent_info_source_id=None),
        ]
    )
    with pytest.raises(RootWatchMissingError):
        await require_root_watch_on_chain(db_session, client, info_source_id=frag_id)


@pytest.mark.asyncio
async def test_require_no_dependents_blocks_when_fragments_exist(db_session):
    root_src = await make_info_source(db_session)
    frag_src = await make_info_source(db_session, parent_info_source_id=root_src.info_source_id)
    info_item_root = await make_info_item(db_session, name="Root Item")
    info_item_frag = await make_info_item(db_session, name="Fragment Item")
    root_watch = await make_watch(
        db_session,
        info_item_id=info_item_root.info_item_id,
        info_source_id=root_src.info_source_id,
    )
    await make_watch(
        db_session,
        info_item_id=info_item_frag.info_item_id,
        info_source_id=frag_src.info_source_id,
    )
    client = MagicMock()
    client.list_info_sources = AsyncMock(
        return_value=MagicMock(
            items=[
                MagicMock(
                    info_source_id=frag_src.info_source_id,
                    parent_info_source_id=root_src.info_source_id,
                ),
            ]
        )
    )
    with pytest.raises(FragmentDependentsExistError) as exc:
        await require_no_fragment_dependents(db_session, client, root_watch)
    assert str(frag_src.info_source_id) in str(exc.value)
