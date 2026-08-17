"""Tests for the publish_watch_status periodic task wrapper (#264).

The publish path itself is covered in tests/core/test_watch_status.py; these
pin the wrapper's env contract (no ``WATCHER_BUS_REDIS_URL`` means a loud
skip — Archiver's panel goes stale and an operator must be able to see why,
never a crash and never a silent pass), the configurable republish cadence,
and the best-effort defer used by mutation paths.
"""

from src.core.bus import BUS_ENABLED_ENV, BUS_REDIS_URL_ENV
from src.workers.watch_status import (
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

    async def test_skips_loudly_when_the_url_is_set_without_the_opt_in(self, monkeypatch, caplog):
        """#262: the URL check alone is no longer the gate.

        This task used to read ``WATCHER_BUS_REDIS_URL`` directly and then
        ``assert client is not None``. With the opt-in gating client
        construction, that pairing turns a stray process's misconfiguration
        into an ``AssertionError`` — or, under ``python -O``, a publish attempt
        on ``None``. It must skip, and say which variable is missing.
        """
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
        monkeypatch.delenv(BUS_ENABLED_ENV, raising=False)
        with caplog.at_level("ERROR", logger="src.workers.watch_status"):
            result = await publish_watch_status()
        assert result == {"skipped": f"{BUS_ENABLED_ENV} is not 1"}
        assert any(BUS_ENABLED_ENV in r.getMessage() for r in caplog.records)


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


class TestDeferCoalescing:
    """CR-2: a cold start applies N announcements in a burst; the queueing
    lock keeps that N deferred republishes, not N queued jobs each publishing
    the full set."""

    async def test_defer_passes_the_queueing_lock(self, monkeypatch):
        captured = {}

        class _Job:
            async def defer_async(self):
                captured["deferred"] = True

        def configure(**kwargs):
            captured.update(kwargs)
            return _Job()

        monkeypatch.setattr("src.workers.watch_status.publish_watch_status.configure", configure)
        await defer_status_republish()
        assert captured["queueing_lock"] == "publish_watch_status"
        assert captured["deferred"] is True

    async def test_already_enqueued_is_a_quiet_coalesce_not_a_warning(self, monkeypatch, caplog):
        from procrastinate.exceptions import AlreadyEnqueued

        class _Job:
            async def defer_async(self):
                raise AlreadyEnqueued("queueing lock held")

        monkeypatch.setattr(
            "src.workers.watch_status.publish_watch_status.configure",
            lambda **kwargs: _Job(),
        )
        with caplog.at_level("WARNING", logger="src.workers.watch_status"):
            await defer_status_republish()
        assert not [r for r in caplog.records if r.levelno >= 30]
