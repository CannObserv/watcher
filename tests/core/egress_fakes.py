"""Fakes for the destination guard's two seams: the resolver and the inner transport."""

import socket
from collections.abc import Sequence

import httpx

PUBLIC_ADDRESS = "93.184.216.34"


def resolver(table: dict[str, Sequence[str]]):
    """A stand-in for getaddrinfo: hostname → the addresses it answers with."""

    async def resolve(host: str, port: int) -> Sequence[str]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return list(table[host])

    return resolve


def never_resolves():
    """A resolver that fails the test if a literal ever takes the DNS path."""

    async def resolve(host: str, port: int) -> Sequence[str]:
        raise AssertionError(f"resolver called for {host}")

    return resolve


class RecordingTransport(httpx.AsyncBaseTransport):
    """Inner transport that records every request that reached it."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[str] = []
        self._response = response
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return self._response or httpx.Response(200)

    async def aclose(self) -> None:
        self.closed = True
