"""Logging adapter — delegates to the shared watcher logging module.

When the Information service extracts to its own repo, this module becomes
its own logging configuration.
"""

from src.core.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
