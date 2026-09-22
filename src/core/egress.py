"""The probe destination guard: what a probe may reach from this host (#305).

``probe_url`` takes an arbitrary URL from an operator and issues ``HEAD`` with
``follow_redirects=True``, returning status code, content type and the full
redirect chain. The capability that grants is not "issue an outbound request" —
it is *reach whatever this host's network position reaches, and report back*.
Both call sites are authenticated (``require_api_key`` on ``/api/v1/probe``,
``get_dashboard_user`` on ``POST /domains``), which bounds who may pull the
trigger; nothing bounded where the barrel pointed. On co-watcher there is
something to reach: ``127.0.0.1:9999`` answers **200** unauthenticated, exeuntu's
socket-activated Shelley agent UI, which the exe.dev proxy authenticates and
loopback bypasses (#304, CannObserv/replicator#97).

**Why a transport and not a check at the top of ``probe_url``.**
``follow_redirects=True`` means the submitted URL does not decide the
destination: a public origin answering ``302 Location: http://127.0.0.1:9999/``
walks around any check on the string the operator typed. ``AsyncClient`` calls
its transport once per hop, so a check here is the cheapest thing that sees
every destination actually contacted rather than only the first one requested.

**The predicate is replicator's, not a second one.** The range table,
:func:`_containing` and the literals-are-not-resolved rule are copied from
``src/worker/egress.py`` in CannObserv/replicator (issue #95, decision in their
#89), which closed the equivalent hole on the ``content.fetch`` path. It is not
importable — ``co-core`` carries no address predicate — so it is copied, and
this comment names the source so the two can be diffed. Two independent
implementations of the same address-set test is how one of them ends up missing
a range.

**Two deliberate divergences from replicator's copy**, both because this is a
one-shot operator probe rather than a retrying worker loop:

- *No env override.* Replicator carries the table in
  ``REPLICATOR_BLOCKED_DESTINATIONS`` so a dev host can widen it. Watcher probes
  nothing on a timer and every URL it probes is public, so the table is
  compiled: there is no configuration, therefore no env file whose absence
  quietly empties it.
- *Resolution failures stay ``httpx`` errors.* Replicator splits them into
  transient and permanent because its loop classifies by exception type for
  retry. Both of watcher's call sites ask one question — could this URL be
  reached — and both already handle ``httpx.HTTPError``, so a name that does not
  resolve keeps surfacing as one.

**An answer the guard cannot check is refused, never passed** (#316, their
#100). The check is a loop over the resolved addresses, so an empty answer, or
one that is not an address, would skip it rather than fail it. ``getaddrinfo``
gives neither, but the resolver is a seam, and the direction a guard fails in
must not rest on its seam's manners. The answer is parsed once, at the
boundary, by :func:`_checkable` — the same shape as replicator's, raising
``httpx.ConnectError`` where theirs raises ``TransientFetchError``.

**The residual, stated rather than closed: DNS rebinding.** The check resolves
and inspects every address, then hands the *name* to the inner transport, which
resolves again — a TOCTOU window an origin controlling its own DNS can aim at.
Closing it means connecting to the pinned address with the ``Host`` header
preserved and certificate verification still keyed to the name, which is more
machinery than this threat justifies: rebinding needs a hostile origin *and* an
authenticated operator aiming a probe at it.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence

import httpx

from src.core.logging import get_logger

logger = get_logger(__name__)

# What a probe may not reach. Every range here is either this host, this host's
# private network, or the tailnet the bus rides — none of which a public site is
# ever served from, so the operational cost of the guard is expected to be
# exactly zero refusals.
#
# ``100.64.0.0/10`` is the sharp one: CGNAT is where Tailscale assigns node
# addresses, so this is the range that stops one node from being made to report
# on another's surfaces.
DEFAULT_BLOCKED_DESTINATIONS: tuple[str, ...] = (
    "0.0.0.0/8",  # "this host on this network" — RFC 1122
    "10.0.0.0/8",  # RFC 1918
    "100.64.0.0/10",  # CGNAT — the tailnet
    "127.0.0.0/8",  # loopback, the whole /8 and not just .0.1
    "169.254.0.0/16",  # link-local, including the cloud metadata address
    "172.16.0.0/12",  # RFC 1918
    "192.168.0.0/16",  # RFC 1918
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved
    "::/128",  # unspecified
    "::1/128",  # loopback
    "fc00::/7",  # unique local
    "fe80::/10",  # link-local
    "ff00::/8",  # multicast
)

# How long the guard's own resolve may take. It exists because resolving here
# moved the resolve *out* of every timeout the probe has: httpcore wraps name
# resolution in the connect timeout (``anyio.fail_after`` around
# ``anyio.connect_tcp``), and a check ahead of the inner transport is ahead of
# that too, so without this a blackholed nameserver parks an operator's request
# for as long as the C resolver retries. Deliberately its own number rather than
# ``PROBE_TIMEOUT``: ``probe`` imports this module, and the two bound different
# operations — 15s is a generous ceiling for a government portal's response.
#
# **10 s, not 5 (#316).** 5 s is glibc's per-try default (``resolv.conf``
# ``timeout:5``; co-watcher's is Tailscale MagicDNS with default options), so a
# cap equal to it fails exactly the resolve one dropped UDP packet makes slow —
# the one libc finishes on its second try, which only *starts* at 5 s. 10 s
# leaves room for that whole second try and stays well under libc's full budget
# (~20 s across two nameservers). Replicator's copy is 10 s for the same
# arithmetic (CannObserv/replicator#100), so the two agree on purpose.
RESOLVE_TIMEOUT = 10.0

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

BLOCKED_NETWORKS: tuple[Network, ...] = tuple(
    ipaddress.ip_network(value, strict=True) for value in DEFAULT_BLOCKED_DESTINATIONS
)


class DestinationRefused(Exception):
    """A probe hop resolved into a range this host does not probe (#305).

    **Deliberately not an ``httpx.HTTPError``.** Both call sites already catch
    that to mean "the URL could not be reached", and a refusal is the opposite
    claim: the destination was reached for, understood, and declined. Sharing
    the base class would have the existing handlers report a refusal as an
    unreachable URL, which is the message the issue asked for a distinct error
    to avoid.
    """


async def resolve_addresses(host: str, port: int) -> Sequence[str]:
    """Resolve ``host`` to every address it answers with.

    Through the running loop's ``getaddrinfo`` rather than ``socket``'s: a
    blocking resolve inside an async route parks the whole process, and this one
    runs in the single uvicorn process that serves the API, the dashboard and
    the worker.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


class GuardedTransport(httpx.AsyncBaseTransport):
    """Refuse a request whose destination resolves into a blocked range.

    Composition rather than a subclass of ``AsyncHTTPTransport``: the inner
    transport is what a test replaces to assert the refusal happened *before*
    anything left the host.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        blocked: tuple[Network, ...] = BLOCKED_NETWORKS,
        resolve: Resolver = resolve_addresses,
        resolve_timeout: float = RESOLVE_TIMEOUT,
    ) -> None:
        self._inner = inner
        self._blocked = blocked
        self._resolve = resolve
        self._resolve_timeout = resolve_timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._refuse_blocked_destination(request.url)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _refuse_blocked_destination(self, url: httpx.URL) -> None:
        host = url.host
        for address in await self._addresses(host, url.port or _default_port(url)):
            network = _containing(address, self._blocked)
            if network is None:
                continue
            logger.warning(
                "probe destination refused",
                extra={"host": host, "address": str(address), "network": str(network)},
            )
            raise DestinationRefused(
                f"{url} resolves to {address}, inside the refused range {network} — "
                f"Watcher does not probe its own host, its private network, or the tailnet"
            )

    async def _addresses(self, host: str, port: int) -> list[Address]:
        """The addresses to check — the literal itself when the URL names one.

        A URL naming an address must not take the resolver path at all: DNS is
        not consulted for a literal, so a guard that resolves first would be
        asking a question with no answer and would have to decide what an
        unresolvable host means. It means nothing here; the address is already
        in hand.
        """
        try:
            literal = _address(host)
        except ValueError:
            return await self._resolve_or_classify(host, port)
        return [literal]

    async def _resolve_or_classify(self, host: str, port: int) -> list[Address]:
        """Resolve, keeping a name failure the kind of failure it used to be.

        Resolving here moves where an unresolvable host surfaces. Before this
        guard it failed inside httpx as a ``ConnectError`` — an
        ``httpx.HTTPError``, which ``/api/v1/probe`` turns into a 422 "URL
        unreachable" and the domain-create form turns into "Could not reach
        URL". A bare ``socket.gaierror`` in its place is neither, and would
        reach the API route unhandled as a 500 (replicator hit this as their
        CR 2, from the other direction: their retry loop dead-lettered good
        commands). Both resolution failures are re-raised as ``ConnectError``
        because that is the honest answer at this surface — the host could not
        be reached — and the distinction replicator needs between them only
        matters to something that will try again.

        **The deadline is this module's, for the same reason** (see
        :data:`RESOLVE_TIMEOUT`). Cancelling the wait does not cancel the
        resolve: ``loop.getaddrinfo`` runs in the default executor, so the
        thread runs to completion and only the waiter gives up — which is the
        point, since the waiter is what an operator is holding a request open
        for.
        """
        try:
            async with asyncio.timeout(self._resolve_timeout):
                answer = await self._resolve(host, port)
        except TimeoutError as exc:
            raise httpx.ConnectTimeout(
                f"{host} did not resolve within {self._resolve_timeout}s"
            ) from exc
        except socket.gaierror as exc:
            raise httpx.ConnectError(f"{host} could not be resolved: {exc}") from exc
        except UnicodeError as exc:
            raise httpx.ConnectError(f"{host} is not an encodable hostname: {exc}") from exc
        return _checkable(host, answer)


def _default_port(url: httpx.URL) -> int:
    return 443 if url.scheme == "https" else 80


def _checkable(host: str, answer: Sequence[str]) -> list[Address]:
    """The resolver's answer as addresses the guard can check, or a refusal (#316).

    **Both refusals close a hole the refusing loop would otherwise leave open.**
    The guard refuses from *inside* a loop over the answer, so an empty answer
    runs it zero times and passes the request unchecked (#305 CR 2); and before
    #316 an answer that did not parse reached :func:`_containing` as ``None`` —
    "in no blocked range" — and passed too. Neither comes from ``getaddrinfo``;
    both can come from a resolver seam that is not ``getaddrinfo``.

    **Emptiness is judged on the list, not on the answer.** ``not <generator>``
    is ``False`` whatever it yields, so testing the answer let an empty iterator
    through the same zero-trip loop; ``or ()`` keeps a ``None`` answer on this
    refusal rather than a ``TypeError``.

    ``ConnectError`` for both, like every other resolution failure here: the
    honest answer at this surface is that the host could not be reached.
    """
    try:
        addresses = [_address(value) for value in answer or ()]
    except ValueError as exc:
        raise httpx.ConnectError(
            f"{host} resolved to something that is not an address: {exc}"
        ) from exc
    if not addresses:
        raise httpx.ConnectError(f"{host} resolved to no addresses")
    return addresses


def _address(value: str) -> Address:
    """Parse ``value`` as the destination it names; ``ValueError`` if it names none.

    An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is the same destination
    spelled differently, and the ``/8`` in the table would not hold it — so it is
    unmapped here, once, for both the literal and the resolved path.
    """
    parsed = ipaddress.ip_address(value)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _containing(address: Address, blocked: tuple[Network, ...]) -> Network | None:
    """The first blocked range holding ``address``, or ``None``.

    Takes an address already parsed, so it has no answer to give about one that
    is not — that question is asked, and refused, at the resolver's boundary in
    :func:`_checkable`. This function answered it with ``None`` until #316: an
    arm #305 called unreachable, and which failed open.
    """
    for network in blocked:
        if address.version == network.version and address in network:
            return network
    return None
