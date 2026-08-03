"""Regression tests: JSON log records carry timestamp, level, and logger name
(#238), and uvicorn's own loggers share the app's JSON formatter (#244).
"""

import json
import logging
import logging.config
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.core.logging import build_json_formatter, configure_logging, get_logger

LOG_CONFIG_PATH = Path("src/core/log_config.json")

# Root plus the three loggers uvicorn ships with propagate=False and its own
# plain-text handlers — the ones --log-config has to reach.
_UVICORN_LOGGER_NAMES = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


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


@pytest.fixture
def restore_logging_tree() -> Iterator[None]:
    """Put root and uvicorn's loggers back after a dictConfig application.

    dictConfig rewrites handlers, propagate, AND level on every logger it
    names; leaking that into later tests would be an order-dependent flake.
    """
    saved = {
        name: (lg.handlers[:], lg.propagate, lg.level)
        for name, lg in ((n, logging.getLogger(n)) for n in _UVICORN_LOGGER_NAMES)
    }
    yield
    for name, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate, lg.level = handlers, propagate, level


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


def test_uvicorn_log_config_is_valid_and_shares_formatter(restore_logging_tree):
    """The uvicorn --log-config file wires uvicorn's loggers through the same
    formatter as the app, and dictConfig accepts it (a malformed file would
    fail the service at boot, not in review)."""
    config = json.loads(LOG_CONFIG_PATH.read_text())

    # Single source of truth: the file builds its formatter from the factory
    # configure_logging() also uses, not a duplicated fmt string.
    assert any(
        f.get("()") == "src.core.logging.build_json_formatter"
        for f in config["formatters"].values()
    )
    # All three uvicorn loggers must be present, else they keep the plain default.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert name in config["loggers"]
        assert config["loggers"][name]["propagate"] is False

    logging.config.dictConfig(config)  # raises on a malformed config


def test_shared_formatter_renders_uvicorn_access_record():
    """A uvicorn.access record formats to JSON with the same fields as app logs
    — the request line lands in `message`, not a plain-text handler."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:0", "GET", "/health", "1.1", 200),
        exc_info=None,
    )
    parsed = json.loads(build_json_formatter().format(record))
    assert parsed["logger"] == "uvicorn.access"
    assert parsed["level"] == "INFO"
    assert parsed["message"] == '127.0.0.1:0 - "GET /health HTTP/1.1" 200'
    assert "timestamp" in parsed
