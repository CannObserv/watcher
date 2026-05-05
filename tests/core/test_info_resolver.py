"""Unit tests for src.core.info_resolver."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.info_resolver import ResolvedInfoSpec, resolve_primary


@pytest.mark.asyncio
async def test_resolve_returns_primary_spec():
    """resolve_primary returns ResolvedInfoSpec with stringified IDs and a dict document."""
    fake_client = MagicMock()
    fake_spec = MagicMock()
    fake_spec.info_spec_id = "01XYZ"
    fake_spec.info_item_id = "01ABC"
    fake_spec.document = {"target": {"url": "https://x"}}
    fake_client.get_primary_info_spec = AsyncMock(return_value=fake_spec)

    resolved = await resolve_primary(fake_client, "01ABC")

    assert isinstance(resolved, ResolvedInfoSpec)
    assert resolved.info_spec_id == "01XYZ"
    assert resolved.info_item_id == "01ABC"
    assert resolved.document["target"]["url"] == "https://x"


@pytest.mark.asyncio
async def test_resolve_with_force_refresh_passes_flag_through():
    """force_refresh=True is forwarded to client.get_primary_info_spec verbatim."""
    fake_client = MagicMock()
    fake_spec = MagicMock()
    fake_spec.info_spec_id = "01XYZ"
    fake_spec.info_item_id = "01ABC"
    fake_spec.document = {"target": {"url": "https://x"}}
    fake_client.get_primary_info_spec = AsyncMock(return_value=fake_spec)

    await resolve_primary(fake_client, "01ABC", force_refresh=True)

    fake_client.get_primary_info_spec.assert_awaited_once_with("01ABC", force_refresh=True)


@pytest.mark.asyncio
async def test_resolve_default_force_refresh_is_false():
    """Default behavior calls SDK with force_refresh=False."""
    fake_client = MagicMock()
    fake_spec = MagicMock()
    fake_spec.info_spec_id = "01XYZ"
    fake_spec.info_item_id = "01ABC"
    fake_spec.document = {"target": {"url": "https://x"}}
    fake_client.get_primary_info_spec = AsyncMock(return_value=fake_spec)

    await resolve_primary(fake_client, "01ABC")

    fake_client.get_primary_info_spec.assert_awaited_once_with("01ABC", force_refresh=False)


@pytest.mark.asyncio
async def test_resolve_coerces_to_dict_via_to_dict_method():
    """SDK's InfoSpecOutDocument wrapper exposes to_dict() — use it."""
    fake_client = MagicMock()
    fake_spec = MagicMock()
    fake_spec.info_spec_id = "01XYZ"
    fake_spec.info_item_id = "01ABC"
    # Simulate the generated wrapper: has .to_dict() returning a plain dict
    fake_doc = MagicMock()
    fake_doc.to_dict = MagicMock(return_value={"schema_version": 1, "target": {"url": "https://x"}})
    # Important: spec=True so hasattr(doc, "to_dict") is True
    fake_spec.document = fake_doc
    fake_client.get_primary_info_spec = AsyncMock(return_value=fake_spec)

    resolved = await resolve_primary(fake_client, "01ABC")

    fake_doc.to_dict.assert_called_once()
    assert resolved.document == {"schema_version": 1, "target": {"url": "https://x"}}
