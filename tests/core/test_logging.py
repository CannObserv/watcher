"""Regression test: JSON log records carry timestamp, level, and logger name (#238)."""

import json
import logging
from datetime import UTC, datetime

from src.core.logging import configure_logging, get_logger


def test_log_record_includes_structured_fields(capsys):
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging()
        get_logger("src.some.module").warning("hello %s", "world")
    finally:
        root.handlers, root.level = saved_handlers, saved_level

    record = json.loads(capsys.readouterr().out)
    assert record["message"] == "hello world"
    assert record["level"] == "WARNING"
    assert record["logger"] == "src.some.module"

    # AGENTS.md convention: all timestamps are ISO 8601 UTC, never naive.
    stamped = datetime.fromisoformat(record["timestamp"])
    assert stamped.tzinfo is not None
    assert stamped.utcoffset() == UTC.utcoffset(None)
