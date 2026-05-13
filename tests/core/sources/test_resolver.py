"""resolve_root_sources_with_children walks parent chain + lists children."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.sources.resolver import (
    ResolvedFragmentSource,
    ResolvedRootSource,
    resolve_root_sources_with_children,
)


@pytest.mark.asyncio
async def test_resolves_root_with_no_fragments():
    client = MagicMock()
    client.get_info_source = AsyncMock(
        return_value=MagicMock(
            info_source_id="01HZZ00000000000000000ROOT",
            parent_info_source_id=None,
            source_spec={
                "target": {"url": "https://example.com"},
                "extraction": {"algorithm": "full_page"},
            },
        )
    )
    client.list_info_sources = AsyncMock(return_value=MagicMock(items=[]))

    resolved = await resolve_root_sources_with_children(
        client, info_source_id="01HZZ00000000000000000ROOT"
    )
    assert isinstance(resolved, ResolvedRootSource)
    assert resolved.url == "https://example.com"
    assert resolved.children == []


@pytest.mark.asyncio
async def test_walks_parent_chain_to_root():
    client = MagicMock()
    client.get_info_source = AsyncMock(
        side_effect=[
            MagicMock(
                info_source_id="01HZZ00000000000000FRAGMENT",
                parent_info_source_id="01HZZ00000000000000000ROOT",
                source_spec={"extraction": {"algorithm": "css", "selector": "#main"}},
            ),
            MagicMock(
                info_source_id="01HZZ00000000000000000ROOT",
                parent_info_source_id=None,
                source_spec={"target": {"url": "https://example.com"}},
            ),
        ]
    )
    client.list_info_sources = AsyncMock(
        return_value=MagicMock(
            items=[
                MagicMock(
                    info_source_id="01HZZ00000000000000FRAGMENT",
                    parent_info_source_id="01HZZ00000000000000000ROOT",
                    source_spec={"extraction": {"algorithm": "css", "selector": "#main"}},
                ),
            ]
        )
    )

    resolved = await resolve_root_sources_with_children(
        client, info_source_id="01HZZ00000000000000FRAGMENT"
    )
    assert resolved.info_source_id == "01HZZ00000000000000000ROOT"
    assert len(resolved.children) == 1
    child = resolved.children[0]
    assert isinstance(child, ResolvedFragmentSource)
    assert child.info_source_id == "01HZZ00000000000000FRAGMENT"
