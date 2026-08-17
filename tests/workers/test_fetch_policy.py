"""Tests for the publish_fetch_policy periodic task wrapper (#245).

The publish path itself is covered in tests/core/test_fetch_policy.py; these pin
the wrapper's env contract: no ``WATCHER_BUS_REDIS_URL`` means a loud skip
(an ERROR record — Replicator falls back to its conservative default, but an
operator must be able to see that the numbers are not travelling), never a
crash and never a silent pass.
"""

from src.core.bus import BUS_ENABLED_ENV, BUS_REDIS_URL_ENV
from src.workers.fetch_policy import publish_fetch_policy


class TestPublishFetchPolicyTask:
    async def test_skips_loudly_without_bus_url(self, monkeypatch, caplog):
        monkeypatch.delenv(BUS_REDIS_URL_ENV, raising=False)
        with caplog.at_level("ERROR", logger="src.workers.fetch_policy"):
            result = await publish_fetch_policy()
        assert result == {"skipped": f"{BUS_REDIS_URL_ENV} not set"}
        assert any(BUS_REDIS_URL_ENV in r.getMessage() for r in caplog.records)

    async def test_skips_loudly_when_the_url_is_set_without_the_opt_in(self, monkeypatch, caplog):
        """#262: a URL without ``WATCHER_BUS_ENABLED=1`` builds no client.

        This task used to read the URL directly and then ``assert client is not
        None``. Once the opt-in gates construction, that pairing turns a stray
        process's misconfiguration into an ``AssertionError`` — or, under
        ``python -O``, a publish attempt on ``None``. Skip, and name the
        variable that is actually missing.
        """
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
        monkeypatch.delenv(BUS_ENABLED_ENV, raising=False)
        with caplog.at_level("ERROR", logger="src.workers.fetch_policy"):
            result = await publish_fetch_policy()
        assert result == {"skipped": f"{BUS_ENABLED_ENV} is not 1"}
        assert any(BUS_ENABLED_ENV in r.getMessage() for r in caplog.records)
