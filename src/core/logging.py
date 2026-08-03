"""Structured JSON logging utilities."""

import logging
import sys

from pythonjsonlogger.json import JsonFormatter


def build_json_formatter() -> JsonFormatter:
    """The single JSON formatter definition for the whole process.

    Referenced by BOTH `configure_logging()` (non-uvicorn entry points) and
    `src/core/log_config.json` (uvicorn's `--log-config`, via the dictConfig
    `"()"` factory key), so app records and uvicorn's own access/error lines
    serialize with one identical schema — no drift, one place to change.

    Keys must be named in the fmt: a bare JsonFormatter() defaults to
    "%(message)s" and emits records with no level, logger, or timestamp (#238).
    """
    return JsonFormatter(
        "%(levelname)s %(name)s %(message)s",
        timestamp=True,
        rename_fields={"levelname": "level", "name": "logger"},
    )


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with JSON formatting. Call once at entry
    points that do NOT run under uvicorn (CLI scripts, alembic env, tools).
    Under uvicorn, `--log-config src/core/log_config.json` configures the whole
    logging tree at boot instead; this call is then equivalent to a no-op (it
    reinstalls an identical root handler), which keeps app logs JSON even if
    someone launches uvicorn without --log-config (#244).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_json_formatter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Use in modules as: logger = get_logger(__name__)"""
    return logging.getLogger(name)
