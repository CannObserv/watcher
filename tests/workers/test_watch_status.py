"""Tests for the publish_watch_status periodic task wrapper (#264).

The publish path itself is covered in tests/core/test_watch_status.py; these
pin the wrapper's env contract (no ``WATCHER_BUS_REDIS_URL`` means a loud
skip — Archiver's panel goes stale and an operator must be able to see why,
never a crash and never a silent pass), the configurable republish cadence,
and the best-effort defer used by mutation paths.
"""

from src.workers.watch_status import (
    BUS_REDIS_URL_ENV,
    DEFAULT_REPUBLISH_CRON,
    REPUBLISH_CRON_ENV,
    _republish_cron,
    defer_status_republish,
    publish_watch_status,
)


class TestPublishWatchStatusTask:
    async def test_skips_loudly_without_bus_url(self, monkeypatch, caplog):
        monkeypatch.delenv(BUS_REDIS_URL_ENV, raising=False)
        with caplog.at_level("ERROR", logger="src.workers.watch_status"):
            result = await publish_watch_status()
        assert result == {"skipped": f"{BUS_REDIS_URL_ENV} not set"}
        assert any(BUS_REDIS_URL_ENV in r.getMessage() for r in caplog.records)


class TestRepublishCron:
    def test_defaults_to_five_minutes(self, monkeypatch):
        monkeypatch.delenv(REPUBLISH_CRON_ENV, raising=False)
        assert _republish_cron() == DEFAULT_REPUBLISH_CRON

    def test_env_overrides_the_period(self, monkeypatch):
        # The republish period is the recovery bound for a dropped frame —
        # deploy-tunable without a code change (#264 done-when).
        monkeypatch.setenv(REPUBLISH_CRON_ENV, "*/15 * * * *")
        assert _republish_cron() == "*/15 * * * *"

    def test_a_malformed_cron_falls_back_loudly(self, monkeypatch, caplog):
        # A typo'd env var must degrade to the default cadence, not kill the
        # worker at import or silence the stream.
        monkeypatch.setenv(REPUBLISH_CRON_ENV, "every 5 minutes")
        with caplog.at_level("ERROR", logger="src.workers.watch_status"):
            assert _republish_cron() == DEFAULT_REPUBLISH_CRON
        assert any(REPUBLISH_CRON_ENV in r.getMessage() for r in caplog.records)


class TestDeferStatusRepublish:
    async def test_a_failed_defer_is_swallowed_with_a_warning(self, monkeypatch, caplog):
        # Best-effort by design: the mutation has already committed and the
        # periodic tick republishes everything anyway, so a failed defer
        # degrades to bounded staleness — it must never fail its caller.
        async def boom():
            raise RuntimeError("no app")

        monkeypatch.setattr(
            "src.workers.watch_status.publish_watch_status.configure",
            lambda: type("C", (), {"defer_async": staticmethod(boom)})(),
        )
        with caplog.at_level("WARNING", logger="src.workers.watch_status"):
            await defer_status_republish()
        assert any("watch-status republish" in r.getMessage() for r in caplog.records)
