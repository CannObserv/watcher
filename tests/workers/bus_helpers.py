"""Shared wiring for the tests that drive a producer against a refusing broker.

Three copies of the same session-factory shim existed — one per broker-failure
module plus the drain's own — and ``_wire_task_bus`` had been copied verbatim
minus the docstring that explains why it monkeypatches the resolver rather than
the env pair. That is the drift shape #289 CR 20 named: the next change to the
wiring convention fixes the copies someone remembers, and the copy that keeps
passing is the one that stopped testing what it names (CR 2).

Not a ``conftest.py``: these are called directly, not requested as fixtures,
and a module import says where they come from.
"""

from collections.abc import Callable
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock


def mock_session_factory(db_session) -> MagicMock:
    """A session factory yielding the test's own session, used once."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


def wire_task_bus(
    module: ModuleType,
    db_session,
    monkeypatch,
    client_factory: Callable[[], Any],
) -> None:
    """Point a periodic publisher's env gate, client and sessions at fakes.

    ``bus_disabled_reason`` is monkeypatched rather than the env set: the task
    asks ``src.core.bus`` (not the URL variable) since #262, and
    ``tests/conftest.py`` clears what it did not set, so setting the pair here
    would be both indirect and undone.
    """
    monkeypatch.setattr(module, "bus_disabled_reason", lambda: None)
    monkeypatch.setattr(module, "get_shared_bus_client", client_factory)
    monkeypatch.setattr(module, "get_session_factory", lambda: mock_session_factory(db_session))
