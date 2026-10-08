"""The suite's outbound-network guard (#353 CR 8).

``integration`` means "the test database and nothing external", and the default
suite means less than that. Step 0 of #353 proved it once, under
``unshare -n``; the guard proves it on every run. An autouse fixture refuses
any ``connect`` that is not loopback, a unix socket, or ``TEST_DATABASE_URL``'s
host — with ``ENETUNREACH``, what the isolated run saw — and fails the test at
teardown even when the code under test swallowed the refusal. ``live`` tests
are exempt; no gate selects them.
"""

import asyncio
import errno
import socket
from types import SimpleNamespace

import pytest

from tests.network_guard import allowed, guards

TEST_NET = "192.0.2.1"  # RFC 5737 TEST-NET-1: routable nowhere, safe to name


class TestAllowed:
    @pytest.mark.parametrize(
        ("family", "address"),
        [
            (socket.AF_INET, ("127.0.0.1", 5432)),
            (socket.AF_INET, ("127.8.9.10", 80)),
            (socket.AF_INET6, ("::1", 5432, 0, 0)),
            (socket.AF_UNIX, "/var/run/postgresql/.s.PGSQL.5432"),
            (socket.AF_UNIX, b"\0abstract"),
        ],
    )
    def test_local_destinations_pass(self, family, address) -> None:
        assert allowed(family, address, frozenset())

    @pytest.mark.parametrize(
        ("family", "address"),
        [
            (socket.AF_INET, (TEST_NET, 443)),
            (socket.AF_INET, ("10.0.0.5", 6379)),  # a broker over a private network
            (socket.AF_INET6, ("2001:db8::1", 443, 0, 0)),
        ],
    )
    def test_anything_else_is_refused(self, family, address) -> None:
        assert not allowed(family, address, frozenset())

    def test_the_test_database_host_passes(self) -> None:
        assert allowed(socket.AF_INET, ("10.0.0.7", 5432), frozenset({"10.0.0.7"}))


class TestGuards:
    def test_an_ordinary_test_is_guarded(self) -> None:
        assert guards(SimpleNamespace(get_closest_marker=lambda name: None))

    def test_a_live_test_is_not(self) -> None:
        node = SimpleNamespace(get_closest_marker=lambda name: object() if name == "live" else None)
        assert not guards(node)


class TestTheGuardIsOn:
    """Behavioural: the autouse fixture is active in this very test."""

    def test_a_blocking_connect_is_refused_and_recorded(self, network_guard) -> None:
        with pytest.raises(OSError) as raised:
            socket.create_connection((TEST_NET, 9), timeout=1)
        assert raised.value.errno == errno.ENETUNREACH
        assert [a for _, a in network_guard.attempts] == [(TEST_NET, 9)]
        network_guard.attempts.clear()  # handled here, so teardown passes

    def test_connect_ex_reports_unreachable(self, network_guard) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            assert sock.connect_ex((TEST_NET, 9)) == errno.ENETUNREACH
        assert len(network_guard.attempts) == 1
        network_guard.attempts.clear()

    async def test_an_asyncio_connect_is_refused_and_recorded(self, network_guard) -> None:
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection(TEST_NET, 9), timeout=2)
        assert len(network_guard.attempts) == 1
        network_guard.attempts.clear()

    def test_loopback_still_reaches_the_kernel(self, network_guard) -> None:
        """Port 1 on loopback is closed: refused by the kernel, not by the guard."""
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", 1), timeout=1)
        assert network_guard.attempts == []
