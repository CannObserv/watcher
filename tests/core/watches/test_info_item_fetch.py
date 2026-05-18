"""Tests for info_item_fetch — InfoItem binding partition + URL resolution."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.watches.info_item_fetch import (
    InfoItemBindings,
    fetch_info_item_bindings,
)


def _info_source_out(info_source_id, url=None, parent=None):
    """Mock an SDK InfoSourceOut. URL is a first-class field; non-NULL for primaries."""
    out = MagicMock()
    out.info_source_id = info_source_id
    out.url = url
    out.parent_info_source_id = parent
    out.source_spec = MagicMock()
    out.source_spec.additional_properties = {}
    return out


def _binding(info_source_id, role):
    """Mock an SDK InfoItemSourceOut. Schema: {info_source_id, role, created_at} only."""
    out = MagicMock()
    out.info_source_id = info_source_id
    out.role = role  # None | 'cross_check' | 'sub_aspect'
    return out


async def test_partitions_primary_cross_check_sub_aspect():
    info_client = AsyncMock()
    info_item = MagicMock()
    info_item.info_item_id = "ITEM"
    info_item.info_item_sources = [
        _binding("P", None),
        _binding("X1", "cross_check"),
        _binding("S1", "sub_aspect"),
        _binding("S2", "sub_aspect"),
    ]
    info_client.get_info_item.return_value = info_item

    sources = {
        "P": _info_source_out("P", url="https://example.com"),
        "X1": _info_source_out("X1", parent="P"),
        "S1": _info_source_out("S1", parent="P"),
        "S2": _info_source_out("S2", parent="P"),
    }
    info_client.get_info_source.side_effect = lambda iid: sources[iid]

    bindings = await fetch_info_item_bindings(info_client, "ITEM")
    assert isinstance(bindings, InfoItemBindings)
    assert bindings.primary.info_source_id == "P"
    assert bindings.primary_url == "https://example.com"
    assert {c.info_source_id for c in bindings.cross_checks} == {"X1"}
    assert {s.info_source_id for s in bindings.sub_aspects} == {"S1", "S2"}
    assert bindings.info_item is info_item


async def test_raises_when_no_primary():
    info_client = AsyncMock()
    info_item = MagicMock()
    info_item.info_item_sources = [_binding("S1", "sub_aspect")]
    info_client.get_info_item.return_value = info_item

    src = _info_source_out("S1", parent="anything")
    info_client.get_info_source.return_value = src

    with pytest.raises(ValueError, match="no active primary"):
        await fetch_info_item_bindings(info_client, "ITEM")


async def test_unknown_role_is_ignored():
    """Forward-compat: an unrecognised role string is logged-and-skipped, not raised."""
    info_client = AsyncMock()
    info_item = MagicMock()
    info_item.info_item_sources = [
        _binding("P", None),
        _binding("M", "mirror"),  # hypothetical future role
    ]
    info_client.get_info_item.return_value = info_item

    sources = {
        "P": _info_source_out("P", url="https://example.com"),
        "M": _info_source_out("M", parent="P"),
    }
    info_client.get_info_source.side_effect = lambda iid: sources[iid]

    bindings = await fetch_info_item_bindings(info_client, "ITEM")
    assert bindings.primary.info_source_id == "P"
    assert bindings.cross_checks == []
    assert bindings.sub_aspects == []
