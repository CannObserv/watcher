"""Regression test: JSON log records carry timestamp, level, and logger name (#238)."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from src.core.logging import configure_logging, get_logger


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    """Put the root logger back after a test reconfigures it.

    configure_logging() replaces the root handler list wholesale (detaching
    pytest's own handlers) and resets the level, so any test that calls it must
    restore both. The call itself stays in the test body: capsys swaps
    sys.stdout per test phase, and configure_logging() binds the handler to
    whichever stream is current — calling it during fixture setup would pin the
    handler to the setup-phase stream, where capsys.readouterr() can't see it.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    root.handlers, root.level = saved_handlers, saved_level


def test_log_record_includes_structured_fields(restore_root_logger, capsys):
    configure_logging()
    get_logger("src.some.module").warning("hello %s", "world")

    record = json.loads(capsys.readouterr().out)
    assert record["message"] == "hello world"
    assert record["level"] == "WARNING"
    assert record["logger"] == "src.some.module"

    # AGENTS.md convention: all timestamps are ISO 8601 UTC, never naive.
    stamped = datetime.fromisoformat(record["timestamp"])
    assert stamped.tzinfo is not None
    assert stamped.utcoffset() == UTC.utcoffset(None)
