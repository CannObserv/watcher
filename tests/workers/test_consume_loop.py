"""The two consume-loop helpers both bus loops share (#289, #436).

The loops' behaviour is pinned end to end in ``test_bus_reconnect.py``; these
pin the helpers themselves and the one property the leaf module exists for —
``registry_reconcile`` reaching them without the blobs consumer's import graph.
"""

import ast
import asyncio
import logging
from pathlib import Path

from src.workers import consume_loop

_REGISTRY = Path(__file__).parents[2] / "src" / "workers" / "registry_reconcile.py"


class TestLogPollRecovery:
    def test_a_failure_run_logs_one_record_and_resets(self, caplog):
        log = logging.getLogger("test.consume_loop")
        with caplog.at_level("INFO", logger="test.consume_loop"):
            assert consume_loop.log_poll_recovery(log, "info.registry", 3) == 0
        [record] = caplog.records
        assert record.getMessage() == "info.registry poll cycle recovered"
        assert record.levelname == "INFO"
        assert record.topic == "info.registry"
        assert record.after_failures == 3

    def test_no_failure_run_logs_nothing(self, caplog):
        log = logging.getLogger("test.consume_loop")
        with caplog.at_level("INFO", logger="test.consume_loop"):
            assert consume_loop.log_poll_recovery(log, "content.blobs", 0) == 0
        assert caplog.records == []


class TestBackOff:
    async def test_stop_cuts_the_wait_short(self):
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(consume_loop.back_off(stop, 60.0), timeout=1)

    async def test_waits_out_the_timeout_without_stop(self):
        await asyncio.wait_for(consume_loop.back_off(asyncio.Event(), 0.01), timeout=1)


def test_the_registry_loop_does_not_import_the_blobs_consumer():
    """The helpers live in a leaf so the registry loop does not pull in
    ``fetch_facts`` → ``fetch_commands`` → the Procrastinate blueprint."""
    tree = ast.parse(_REGISTRY.read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "src.workers.fetch_facts" not in imported
    assert "src.workers.consume_loop" in imported
