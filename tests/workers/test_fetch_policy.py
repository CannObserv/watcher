"""Tests for the stopped publish_fetch_policy task (#374, F1 part 1).

F1 hands ``content.fetch-policy`` to Archiver. Design D8 allows one producer,
and no ACL refuses a second writer, so the rule is held by sequencing alone:
Watcher's producer stops first. The task stays registered so a job already in
the queue drains as a no-op instead of failing as an unknown task; it is no
longer periodic and publishes nothing. Part 2 deletes the module.
"""

import pytest

import src.workers.fetch_policy as fetch_policy_mod
from src.core.models.domain import Domain
from src.workers import get_app, reset_app
from src.workers.fetch_policy import publish_fetch_policy
from tests.workers.bus_helpers import mock_session_factory


class _RecordingClient:
    """Stands in for the shared bus client; records every ``XADD``."""

    def __init__(self) -> None:
        self.xadds: list[tuple] = []

    async def xadd(self, *args, **kwargs):
        self.xadds.append((args, kwargs))
        return b"0-1"


class TestTheProducerIsStopped:
    def setup_method(self):
        reset_app()

    def teardown_method(self):
        reset_app()

    def test_the_task_stays_registered_so_a_queued_job_drains(self):
        assert "publish_fetch_policy" in get_app().tasks

    def test_the_task_is_no_longer_periodic(self):
        periodic = {name for name, _ in get_app().periodic_registry.periodic_tasks}
        assert "publish_fetch_policy" not in periodic

    @pytest.mark.integration
    async def test_a_run_publishes_nothing_with_the_bus_enabled(
        self, db_session, monkeypatch, caplog
    ):
        """The pre-F1 task would have published this domain's policy here.

        Wired with ``raising=False`` because the stopped module no longer
        imports the bus at all: the wiring is what the old body needed to
        publish, so the test fails against it rather than passing vacuously.
        """
        db_session.add(Domain(name="f1-stopped.example", min_interval=3.0))
        await db_session.flush()
        client = _RecordingClient()
        for name, value in {
            "bus_disabled_reason": lambda: None,
            "get_shared_bus_client": lambda: client,
            "get_session_factory": lambda: mock_session_factory(db_session),
        }.items():
            monkeypatch.setattr(fetch_policy_mod, name, value, raising=False)

        with caplog.at_level("INFO", logger="src.workers.fetch_policy"):
            result = await publish_fetch_policy()

        assert client.xadds == []
        assert result == {"stopped": "F1 (#374)"}
        assert [r.getMessage() for r in caplog.records] == ["fetch-policy producer stopped (F1)"]
