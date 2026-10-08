"""The suite's outbound-network guard (#353 CR 8); wired up in ``tests/conftest.py``.

Patches ``socket.socket.connect`` and ``connect_ex`` for the length of each
test. A destination that is not loopback, a unix socket, or an address of
``TEST_DATABASE_URL``'s host is refused with ``ENETUNREACH`` — what #353's
``unshare -n`` premise check saw — and recorded, so the fixture can fail the
test even when the code under test swallowed the refusal. asyncio's
``sock_connect`` and ``ssl`` both go through ``socket.socket.connect``.

``live`` tests are exempt: they exist to reach a real service, and no gate
selects them. Guarded by ``tests/test_network_guard.py``.
"""

import errno
import ipaddress
import socket
from dataclasses import dataclass, field
from functools import cache
from typing import Any

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


def allowed(family: int, address: Any, extra_hosts: frozenset[str]) -> bool:
    """Return whether a ``connect`` to ``address`` stays on this machine (or the test DB)."""
    if family == getattr(socket, "AF_UNIX", None):
        return True
    if family not in (socket.AF_INET, socket.AF_INET6):
        return False
    host = address[0]
    if host in extra_hosts:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # a hostname reaching connect() unresolved: not ours to allow


@cache
def database_hosts(host: str | None) -> frozenset[str]:
    """Resolve the test database's host once; loopback needs no entry."""
    if not host:
        return frozenset()
    try:
        return frozenset(info[4][0] for info in socket.getaddrinfo(host, None))
    except OSError:
        return frozenset()


def guards(node: Any) -> bool:
    """Return whether the guard applies to this test item."""
    return node.get_closest_marker("live") is None


@dataclass
class NetworkGuard:
    """What one test tried to reach and was refused."""

    extra_hosts: frozenset[str]
    attempts: list[tuple[int, Any]] = field(default_factory=list)

    def _refuse(self, sock: socket.socket, address: Any) -> bool:
        if allowed(sock.family, address, self.extra_hosts):
            return False
        self.attempts.append((sock.family, address))
        return True

    def install(self, monkeypatch: Any) -> None:
        """Patch ``connect``/``connect_ex``; ``monkeypatch`` undoes it at teardown."""
        guard = self

        def connect(sock: socket.socket, address: Any) -> None:
            if guard._refuse(sock, address):
                raise OSError(errno.ENETUNREACH, f"test network guard (#353): {address!r}")
            return _ORIGINAL_CONNECT(sock, address)

        def connect_ex(sock: socket.socket, address: Any) -> int:
            if guard._refuse(sock, address):
                return errno.ENETUNREACH
            return _ORIGINAL_CONNECT_EX(sock, address)

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
