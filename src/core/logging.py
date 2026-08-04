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


class ColorMessageFilter(logging.Filter):
    """Drop uvicorn's `color_message` extra before anything serializes it.

    uvicorn logs its lifecycle lines with an ANSI-coloured duplicate of the
    message attached as `extra={"color_message": ...}` for its own colour-aware
    formatter, and every extra reaches the JSON payload (#246). Listed on each
    uvicorn logger in `src/core/log_config.json` — not on the stdout handler
    and not via the formatter's reserved_attrs: those scope the strip to one
    sink, so a handler that builds its payload from `record.__dict__` (OTel's
    LoggingHandler does, against a reserved list that omits `color_message`)
    would resurrect the field silently. A filter on the logger mutates the
    record once at its source, before any handler reads it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Strip the extra if present. Never drops a record."""
        record.__dict__.pop("color_message", None)
        return True


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
