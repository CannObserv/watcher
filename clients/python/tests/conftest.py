"""Test fixtures for information-client."""

import pytest
from information_client import InformationClient

BASE_URL = "http://information.test"
API_KEY = "test-key"


@pytest.fixture
async def client():
    async with InformationClient(base_url=BASE_URL, api_key=API_KEY, cache_ttl_seconds=60.0) as c:
        yield c
