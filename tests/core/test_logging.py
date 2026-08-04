"""Regression tests: JSON log records carry timestamp, level, and logger name
(#238), uvicorn's own loggers share the app's JSON formatter (#244), and
uvicorn's `color_message` extra never reaches the payload (#246).
"""

import json
import logging
import logging.config
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.core.logging import (
    ColorMessageFilter,
    build_json_formatter,
    configure_logging,
    get_logger,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_CONFIG_RELPATH = "src/core/log_config.json"
LOG_CONFIG_PATH = REPO_ROOT / LOG_CONFIG_RELPATH

# The two sanctioned uvicorn launch paths (AGENTS.md → Server Lifecycle). Both
# must pass --log-config; the file alone is inert without the flag.
LAUNCH_PATHS = ("deploy/watcher.service", "scripts/dev_server.sh")

# The three loggers uvicorn ships with propagate=False and its own plain-text
# handlers — the ones --log-config has to reach.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
# Every logger dictConfig rewrites: those three plus root ("") for the app's
# own records.
_DICTCONFIG_MANAGED_LOGGERS = ("", *_UVICORN_LOGGERS)


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

    dictConfig rewrites handlers, propagate, level AND filters on every logger
    it names; leaking any of them into later tests would be an order-dependent
    flake (the strip_color_message filter would keep mutating records long after
    this test — #246).
    """
    saved = {}
    for name in _DICTCONFIG_MANAGED_LOGGERS:
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.propagate, lg.level, lg.filters[:])
    yield
    for name, (handlers, propagate, level, filters) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate, lg.level, lg.filters = (
            handlers,
            propagate,
            level,
            filters,
        )


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
    for name in _UVICORN_LOGGERS:
        assert name in config["loggers"]
        assert config["loggers"][name]["propagate"] is False
        # Placement matters, not just effect: the strip has to sit on each
        # *logger* so the record is cleaned at its source. A logger's filters
        # run only in Logger.handle() for records logged through that logger —
        # propagation walks ancestors' handlers, never their filters — so
        # listing it on the parent `uvicorn` alone would never see a
        # `uvicorn.error` record. Moving it to the stdout handler (or to the
        # formatter's reserved_attrs) would still pass an output-only
        # assertion, yet resurrect the field for any handler that serializes
        # record.__dict__ directly, e.g. OTel's LoggingHandler (#246).
        assert "strip_color_message" in config["loggers"][name]["filters"]

    assert config["filters"]["strip_color_message"]["()"] == "src.core.logging.ColorMessageFilter"

    logging.config.dictConfig(config)  # raises on a malformed config


@pytest.mark.parametrize("launch_path", LAUNCH_PATHS)
def test_launch_paths_pass_log_config(launch_path: str):
    """Both sanctioned uvicorn launch paths must pass --log-config.

    The dictConfig file is inert without the flag: uvicorn keeps its own
    plain-text handlers and journald goes back to mixed formats (#244). The
    other tests here pin the file's *contents*, so only this one fails when the
    flag is dropped from a launch command — the same drift class
    tests/deploy/test_installed_unit_matches_repo.py guards for the unit as a
    whole (#233).
    """
    text = (REPO_ROOT / launch_path).read_text()
    assert f"--log-config {LOG_CONFIG_RELPATH}" in text, (
        f"{launch_path} launches uvicorn without --log-config {LOG_CONFIG_RELPATH}; "
        "uvicorn's own loggers would emit plain text alongside the JSON records."
    )


def test_log_config_resolves_from_the_repo_root():
    """The --log-config flag is CWD-relative by design (see the comment at each
    call site), so the path must resolve from the repo root — the directory
    both launch paths run uvicorn from."""
    assert LOG_CONFIG_PATH.is_file()


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


def _uvicorn_lifecycle_record() -> logging.LogRecord:
    """A `uvicorn.error` startup line as uvicorn emits it: the plain message
    plus an ANSI-coloured duplicate attached via extra= for uvicorn's own
    colour-aware formatter (server.py / config.py / the --reload supervisors).
    """
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Started server process [%d]",
        args=(4066888,),
        exc_info=None,
    )
    record.color_message = "Started server process [\033[36m%d\033[0m]"
    return record


def test_color_message_filter_strips_the_extra_from_the_record():
    """The strip mutates the record itself, so it holds for every sink — not
    just for a formatter that happens to omit the field (#246)."""
    record = _uvicorn_lifecycle_record()
    assert ColorMessageFilter().filter(record) is True
    assert not hasattr(record, "color_message")
    assert "color_message" not in record.__dict__


def test_color_message_filter_keeps_records_without_the_extra():
    """Never drops a record: access lines pass no extra= at all."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    assert ColorMessageFilter().filter(record) is True
    assert record.getMessage() == "hello"


def test_filtered_uvicorn_lifecycle_record_serializes_without_color_message():
    """End state: the JSON payload carries the four contract keys and no ANSI
    bytes — the polluted duplicate is gone (#246)."""
    record = _uvicorn_lifecycle_record()
    ColorMessageFilter().filter(record)

    rendered = build_json_formatter().format(record)
    assert "\033" not in rendered
    parsed = json.loads(rendered)
    assert "color_message" not in parsed
    assert parsed["message"] == "Started server process [4066888]"
    assert parsed["logger"] == "uvicorn.error"
    assert parsed["level"] == "INFO"
