"""Tests for the publish_fetch_policy periodic task wrapper (#245).

The publish path itself is covered in tests/core/test_fetch_policy.py; these pin
the wrapper's env contract: no ``WATCHER_BUS_REDIS_URL`` means a loud skip
(an ERROR record — Replicator falls back to its conservative default, but an
operator must be able to see that the numbers are not travelling), never a
crash and never a silent pass.
"""

from src.workers.fetch_policy import BUS_REDIS_URL_ENV, publish_fetch_policy


class TestPublishFetchPolicyTask:
    async def test_skips_loudly_without_bus_url(self, monkeypatch, caplog):
        monkeypatch.delenv(BUS_REDIS_URL_ENV, raising=False)
        with caplog.at_level("ERROR", logger="src.workers.fetch_policy"):
            result = await publish_fetch_policy()
        assert result == {"skipped": f"{BUS_REDIS_URL_ENV} not set"}
        assert any(BUS_REDIS_URL_ENV in r.getMessage() for r in caplog.records)
