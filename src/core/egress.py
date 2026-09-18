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
    ) -> None:
        self._inner = inner
        self._blocked = blocked
        self._resolve = resolve

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
                extra={"host": host, "address": address, "network": str(network)},
            )
            raise DestinationRefused(
                f"{url} resolves to {address}, inside the refused range {network} — "
                f"Watcher does not probe its own host, its private network, or the tailnet"
            )

    async def _addresses(self, host: str, port: int) -> Sequence[str]:
        """The addresses to check — the literal itself when the URL names one.

        A URL naming an address must not take the resolver path at all: DNS is
        not consulted for a literal, so a guard that resolves first would be
        asking a question with no answer and would have to decide what an
        unresolvable host means. It means nothing here; the address is already
        in hand.
        """
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return await self._resolve_or_classify(host, port)
        return [host]

    async def _resolve_or_classify(self, host: str, port: int) -> Sequence[str]:
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
        """
        try:
            return await self._resolve(host, port)
        except socket.gaierror as exc:
            raise httpx.ConnectError(f"{host} could not be resolved: {exc}") from exc
        except UnicodeError as exc:
            raise httpx.ConnectError(f"{host} is not an encodable hostname: {exc}") from exc


def _default_port(url: httpx.URL) -> int:
    return 443 if url.scheme == "https" else 80


def _containing(address: str, blocked: tuple[Network, ...]) -> Network | None:
    """The first blocked range holding ``address``, or ``None``.

    **The unparseable branch is unreachable, and says so rather than implying a
    policy.** Both callers hand this a string that has already parsed: a URL
    literal checked by ``_addresses``, or an address ``getaddrinfo`` returned.
    There is no input that reaches the ``except`` below, so it is not a
    fail-open decision about unmodelled destinations — it is the arm that keeps
    a guard from raising ``ValueError`` out of a transport if that ever stops
    being true. If it starts executing, the bug is upstream of here.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - getaddrinfo does not produce these
        return None
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) is the same destination
    # spelled differently, and the /8 above would not hold it.
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    for network in blocked:
        if parsed.version == network.version and parsed in network:
            return network
    return None
