"""TTL cache behaviour for get_primary_info_spec."""

import httpx
import pytest
import respx
from information_client import InformationClient, NotFound

BASE_URL = "http://information.test"


def _spec_payload(info_spec_id: str = "01HZZ00000000000000000000B") -> dict:
    return {
        "info_spec_id": info_spec_id,
        "info_item_id": "01HZZ00000000000000000000A",
        "schema_version": 1,
        "document": {
            "schema_version": 1,
            "target": {"url": "https://example.com"},
            "extraction": {"algorithm": "css", "selector": ".x"},
            "fingerprint": {"algorithm": "sha256"},
        },
        "priority": 1,
        "active": True,
        "created_at": "2026-05-04T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_primary_cache_hit_avoids_second_request():
    async with InformationClient(base_url=BASE_URL, api_key="k", cache_ttl_seconds=60.0) as client:
        with respx.mock:
            route = respx.get(
                f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
            ).mock(return_value=httpx.Response(200, json=_spec_payload()))

            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            await client.get_primary_info_spec("01HZZ00000000000000000000A")

            assert route.call_count == 1


@pytest.mark.asyncio
async def test_primary_cache_force_refresh_skips_cache():
    async with InformationClient(base_url=BASE_URL, api_key="k", cache_ttl_seconds=60.0) as client:
        with respx.mock:
            route = respx.get(
                f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
            ).mock(return_value=httpx.Response(200, json=_spec_payload()))

            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            await client.get_primary_info_spec("01HZZ00000000000000000000A", force_refresh=True)

            assert route.call_count == 2


@pytest.mark.asyncio
async def test_primary_cache_invalidate_clears_entry():
    async with InformationClient(base_url=BASE_URL, api_key="k", cache_ttl_seconds=60.0) as client:
        with respx.mock:
            route = respx.get(
                f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
            ).mock(return_value=httpx.Response(200, json=_spec_payload()))

            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            client.invalidate_primary_cache("01HZZ00000000000000000000A")
            await client.get_primary_info_spec("01HZZ00000000000000000000A")

            assert route.call_count == 2


@pytest.mark.asyncio
async def test_primary_cache_invalidate_all():
    async with InformationClient(base_url=BASE_URL, api_key="k", cache_ttl_seconds=60.0) as client:
        with respx.mock:
            route_a = respx.get(
                f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
            ).mock(return_value=httpx.Response(200, json=_spec_payload()))
            route_b = respx.get(
                f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000B/primary-info-spec"
            ).mock(
                return_value=httpx.Response(200, json=_spec_payload("01HZZ0000000000000000000XX"))
            )

            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            await client.get_primary_info_spec("01HZZ00000000000000000000B")
            client.invalidate_primary_cache()  # all
            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            await client.get_primary_info_spec("01HZZ00000000000000000000B")

            assert route_a.call_count == 2
            assert route_b.call_count == 2


@pytest.mark.asyncio
async def test_primary_cache_expiry_triggers_refetch(monkeypatch):
    import time as time_mod

    fake_time = [1000.0]

    def fake_monotonic():
        return fake_time[0]

    monkeypatch.setattr(time_mod, "monotonic", fake_monotonic)

    async with InformationClient(base_url=BASE_URL, api_key="k", cache_ttl_seconds=60.0) as client:
        with respx.mock:
            route = respx.get(
                f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
            ).mock(return_value=httpx.Response(200, json=_spec_payload()))

            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            fake_time[0] += 30  # within TTL
            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            assert route.call_count == 1

            fake_time[0] += 31  # now past 60s
            await client.get_primary_info_spec("01HZZ00000000000000000000A")
            assert route.call_count == 2


@pytest.mark.asyncio
async def test_primary_cache_does_not_cache_errors():
    async with InformationClient(base_url=BASE_URL, api_key="k", cache_ttl_seconds=60.0) as client:
        with respx.mock:
            route = respx.get(
                f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
            ).mock(return_value=httpx.Response(404, json={"detail": "x"}))

            with pytest.raises(NotFound):
                await client.get_primary_info_spec("01HZZ00000000000000000000A")
            with pytest.raises(NotFound):
                await client.get_primary_info_spec("01HZZ00000000000000000000A")

            assert route.call_count == 2  # no caching of errors
