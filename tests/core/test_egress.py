"""Unit tests for the probe destination guard (#305)."""

import asyncio
import ipaddress
import re

import httpx
import pytest

from src.core.egress import (
    BLOCKED_NETWORKS,
    DEFAULT_BLOCKED_DESTINATIONS,
    RESOLVE_TIMEOUT,
    DestinationRefused,
    GuardedTransport,
    resolve_addresses,
)
from tests.core.egress_fakes import (
    PUBLIC_ADDRESS,
    RecordingTransport,
    never_resolves,
    resolver,
)


async def _head(transport: GuardedTransport, url: str, *, follow_redirects: bool = False):
    async with httpx.AsyncClient(transport=transport, follow_redirects=follow_redirects) as client:
        return await client.head(url)


def _refused_in(network: str) -> str:
    """The refusal naming ``network`` — escaped, since a range's dots are regex wildcards."""
    return re.escape(f"refused range {network} ")


class TestDenySet:
    def test_the_deny_set_parses(self):
        assert len(BLOCKED_NETWORKS) == len(DEFAULT_BLOCKED_DESTINATIONS)

    @pytest.mark.parametrize(
        "cidr",
        [
            "0.0.0.0/8",
            "10.0.0.0/8",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "224.0.0.0/4",
            "240.0.0.0/4",
            "::/128",
            "::1/128",
            "64:ff9b:1::/48",
            "fc00::/7",
            "fe80::/10",
            "ff00::/8",
        ],
    )
    def test_every_range_replicator_refuses_is_in_the_table(self, cidr):
        """The address set is replicator#95's, not a second one (#305)."""
        assert cidr in DEFAULT_BLOCKED_DESTINATIONS


class TestRefusedLiterals:
    @pytest.mark.parametrize(
        ("url", "family"),
        [
            ("http://127.0.0.1:9999/", "loopback"),
            ("http://127.1.2.3/", "loopback, the whole /8"),
            ("http://[::1]:9999/", "IPv6 loopback"),
            ("http://10.1.2.3/", "RFC 1918"),
            ("http://172.16.4.5/", "RFC 1918"),
            ("http://192.168.1.1/", "RFC 1918"),
            ("http://169.254.169.254/", "link-local / cloud metadata"),
            ("http://[fe80::1]/", "IPv6 link-local"),
            ("http://[fc00::1]/", "unique local"),
            ("http://100.64.1.2/", "CGNAT — the tailnet"),
            ("http://0.0.0.0/", "this host on this network"),
            ("http://224.0.0.1/", "multicast"),
            ("http://240.0.0.1/", "reserved"),
            ("http://[::ffff:127.0.0.1]/", "IPv4-mapped loopback"),
        ],
    )
    async def test_each_refused_family_as_the_submitted_url(self, url, family):
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=never_resolves())

        with pytest.raises(DestinationRefused):
            await _head(transport, url)

        assert inner.requests == [], f"{family} left the host"

    async def test_a_literal_is_never_resolved(self):
        """DNS is not consulted for an address literal — the address is in hand."""
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=never_resolves())

        response = await _head(transport, f"http://{PUBLIC_ADDRESS}/")

        assert response.status_code == 200
        assert inner.requests == [f"http://{PUBLIC_ADDRESS}/"]


class TestRefusedNames:
    async def test_a_hostname_resolving_to_loopback_is_refused(self):
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=resolver({"localhost": ["127.0.0.1", "::1"]}))

        with pytest.raises(DestinationRefused):
            await _head(transport, "http://localhost:9999/")

        assert inner.requests == []

    async def test_a_name_resolving_to_ipv4_mapped_loopback_is_refused(self):
        """``::ffff:127.0.0.1`` is loopback spelled as IPv6, on the resolved path too.

        Since #316 each path unmaps on its own call to ``_address``, where
        ``_containing`` once did it for both; the literal case in
        ``TestRefusedLiterals`` cannot see ``_checkable`` parsing with a bare
        ``ipaddress.ip_address`` and letting this through (CR 1).
        """
        inner = RecordingTransport()
        transport = GuardedTransport(
            inner, resolve=resolver({"mapped.example.com": ["::ffff:127.0.0.1"]})
        )

        with pytest.raises(DestinationRefused, match="127.0.0.0/8"):
            await _head(transport, "http://mapped.example.com:9999/")

        assert inner.requests == []

    async def test_every_resolved_address_is_checked_not_just_the_first(self):
        """A name answering with one public and one private address is refused."""
        inner = RecordingTransport()
        transport = GuardedTransport(
            inner, resolve=resolver({"split.example.com": [PUBLIC_ADDRESS, "10.0.0.7"]})
        )

        with pytest.raises(DestinationRefused):
            await _head(transport, "https://split.example.com/")

        assert inner.requests == []

    async def test_a_public_name_probes_normally(self):
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=resolver({"example.com": [PUBLIC_ADDRESS]}))

        response = await _head(transport, "https://example.com/page")

        assert response.status_code == 200
        assert inner.requests == ["https://example.com/page"]


# IPv6 forms that embed an IPv4 address (#402, porting CannObserv/replicator#156).
#
# The mapped form is one of a class. Each row is an IPv6 address carrying a
# blocked IPv4 address, and the range the refusal must name — the IPv4 one, so a
# refusal for some other reason (a future IPv6 row, say) fails the test rather
# than passing it. Replicator's rows, unchanged, so the two suites stay diffable.
EMBEDDING_A_BLOCKED_ADDRESS = [
    ("NAT64 well-known prefix, RFC 6052", "64:ff9b::a00:7", "10.0.0.0/8"),
    ("NAT64 well-known prefix, loopback", "64:ff9b::7f00:1", "127.0.0.0/8"),
    ("local-use NAT64, RFC 8215", "64:ff9b:1::a00:7", "64:ff9b:1::/48"),
    ("IPv4-compatible, RFC 4291", "::7f00:1", "127.0.0.0/8"),
    ("SIIT IPv4-translated, RFC 2765", "::ffff:0:a00:7", "10.0.0.0/8"),
    ("6to4, RFC 3056", "2002:7f00:1::", "127.0.0.0/8"),
    ("6to4, the metadata address", "2002:a9fe:a9fe::1", "169.254.0.0/16"),
    ("Teredo client, RFC 4380", "2001:0:4136:e378:8000:ffff:f5ff:fff8", "10.0.0.0/8"),
    ("Teredo server", "2001:0:a00:7:8000:ffff:a247:27dd", "10.0.0.0/8"),
    ("ISATAP, RFC 5214", "2600:1f18::5efe:a00:7", "10.0.0.0/8"),
    ("ISATAP, u bit set", "2600:1f18::200:5efe:a00:7", "10.0.0.0/8"),
]

# The same forms carrying a public address, which the guard must let through.
EMBEDDING_A_PUBLIC_ADDRESS = [
    ("NAT64 well-known prefix", "64:ff9b::5db8:d822"),
    ("IPv4-compatible", "::5db8:d822"),
    ("SIIT IPv4-translated", "::ffff:0:5db8:d822"),
    ("6to4", "2002:5db8:d822::1"),
    ("Teredo", "2001:0:4136:e378:8000:ffff:a247:27dd"),
    ("ISATAP", "2600:1f18::200:5efe:5db8:d822"),
]


def _embedded_case(path: str, address: str, *, port: str = "") -> tuple[str, dict]:
    """The URL to probe and the resolver table, for the literal or the resolved path."""
    if path == "literal":
        return f"http://[{address}]{port}/", {}
    return f"http://embedded.example.com{port}/", {"embedded.example.com": [address]}


class TestEmbeddedIPv4:
    @pytest.mark.parametrize("path", ["literal", "resolved"])
    @pytest.mark.parametrize(
        ("label", "address", "refused"),
        EMBEDDING_A_BLOCKED_ADDRESS,
        ids=[row[0] for row in EMBEDDING_A_BLOCKED_ADDRESS],
    )
    async def test_an_ipv6_address_embedding_a_blocked_one_is_refused(
        self, path, label, address, refused
    ):
        """An IPv6 address carrying ``10.0.0.7`` reaches ``10.0.0.7`` through a translator.

        ``_containing`` compares versions, so none of these is ever tested against
        the IPv4 row holding what it embeds. Latent on co-watcher — no global IPv6
        route, no tunnel link, no DNS64 — and a bypass of #305 on a host with
        NAT64/DNS64 or a 6to4 relay.
        """
        url, table = _embedded_case(path, address, port=":9999")
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=resolver(table))

        with pytest.raises(DestinationRefused, match=_refused_in(refused)) as caught:
            await _head(transport, url)

        assert address in str(caught.value), label
        assert inner.requests == [], f"{label} left the host"

    async def test_the_refusal_names_the_address_and_what_it_embeds(self):
        """The operator sees the IPv6 address they asked for and why it was refused."""
        transport = GuardedTransport(RecordingTransport(), resolve=never_resolves())

        with pytest.raises(DestinationRefused) as caught:
            await _head(transport, "http://[64:ff9b::a00:7]/")

        assert "resolves to 64:ff9b::a00:7 (embedding 10.0.0.7), inside" in str(caught.value)

    @pytest.mark.parametrize("path", ["literal", "resolved"])
    @pytest.mark.parametrize(
        ("label", "address"),
        EMBEDDING_A_PUBLIC_ADDRESS,
        ids=[row[0] for row in EMBEDDING_A_PUBLIC_ADDRESS],
    )
    async def test_an_ipv6_address_embedding_a_public_one_passes_unchanged(
        self, path, label, address
    ):
        """Checked, never substituted: the request still goes to the IPv6 destination.

        As IPv4, a public NAT64 destination is unreachable from an IPv6-only host,
        and 6to4's embedded address is the site's relay router, not the host
        behind it. Only the mapped form names the same destination.
        """
        url, table = _embedded_case(path, address)
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=resolver(table))

        response = await _head(transport, url)

        assert response.status_code == 200, label
        assert inner.requests == [url], f"{label} was rewritten"

    @pytest.mark.parametrize(
        ("label", "address", "refused"),
        [
            ("ISATAP under link-local", "fe80::5efe:5db8:d822", "fe80::/10"),
            ("ISATAP under ULA", "fd00::200:5efe:5db8:d822", "fc00::/7"),
            ("local-use NAT64, its row", "64:ff9b:1::5db8:d822", "64:ff9b:1::/48"),
        ],
    )
    async def test_the_ipv6_address_itself_is_still_checked(self, label, address, refused):
        """Unwrapping adds a check; it does not replace the IPv6 one.

        The ISATAP rows unwrap to a public address inside a blocked IPv6 range, so
        a change that substituted the embedded address — as ``_address`` does for
        the mapped form — would pass them. The local-use row pins the table: its
        prefix is refused even where the low 32 bits are public.
        """
        transport = GuardedTransport(RecordingTransport(), resolve=never_resolves())

        with pytest.raises(DestinationRefused, match=_refused_in(refused)):
            await _head(transport, f"http://[{address}]/")

    async def test_a_replaced_table_keeps_the_unwrap(self):
        """The ``blocked=`` argument *replaces* the table, so an IPv6 row per
        translator prefix would vanish from any caller that passes one. The unwrap
        holds whatever the table says: naming only ``10.0.0.0/8`` still refuses its
        NAT64 spelling. Replicator's case is its env override; watcher's is this
        constructor argument.
        """
        transport = GuardedTransport(
            RecordingTransport(),
            blocked=(ipaddress.ip_network("10.0.0.0/8"),),
            resolve=never_resolves(),
        )

        with pytest.raises(DestinationRefused, match=_refused_in("10.0.0.0/8")):
            await _head(transport, "http://[64:ff9b::a00:7]/")


class TestRedirectHops:
    async def test_a_public_url_redirecting_into_loopback_is_refused(self):
        """The case the guard exists for — a top-of-function check passes it (#305)."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/"})

        transport = GuardedTransport(
            httpx.MockTransport(handler),
            resolve=resolver({"origin.example.com": [PUBLIC_ADDRESS]}),
        )

        with pytest.raises(DestinationRefused):
            await _head(transport, "https://origin.example.com/", follow_redirects=True)

        assert seen == ["https://origin.example.com/"], "the loopback hop left the host"

    async def test_a_redirect_to_an_embedded_literal_is_refused_at_the_hop(self):
        """The redirect hop is where a public URL becomes an IPv6 literal (#402)."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://[64:ff9b::7f00:1]:9999/"})

        transport = GuardedTransport(
            httpx.MockTransport(handler),
            resolve=resolver({"origin.example.com": [PUBLIC_ADDRESS]}),
        )

        with pytest.raises(DestinationRefused, match=_refused_in("127.0.0.0/8")):
            await _head(transport, "https://origin.example.com/", follow_redirects=True)

        assert seen == ["https://origin.example.com/"], "the embedded hop left the host"

    async def test_a_redirect_to_a_public_url_still_follows(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "origin.example.com":
                return httpx.Response(301, headers={"location": "https://www.example.com/page"})
            return httpx.Response(200)

        transport = GuardedTransport(
            httpx.MockTransport(handler),
            resolve=resolver(
                {"origin.example.com": [PUBLIC_ADDRESS], "www.example.com": [PUBLIC_ADDRESS]}
            ),
        )

        response = await _head(transport, "https://origin.example.com/", follow_redirects=True)

        assert response.status_code == 200
        assert str(response.url) == "https://www.example.com/page"


class TestResolutionFailures:
    async def test_an_unresolvable_host_stays_an_httpx_error(self):
        """Resolving here moved where a name failure surfaces; callers read the type."""
        transport = GuardedTransport(RecordingTransport(), resolve=resolver({}))

        with pytest.raises(httpx.HTTPError):
            await _head(transport, "https://nope.example.com/")

    async def test_an_unencodable_hostname_stays_an_httpx_error(self):
        async def resolve(host: str, port: int):
            raise UnicodeError("label too long")

        transport = GuardedTransport(RecordingTransport(), resolve=resolve)

        with pytest.raises(httpx.HTTPError):
            await _head(transport, "https://bad.example.com/")

    async def test_a_hanging_resolve_is_bounded(self):
        """The guard resolves ahead of httpx, so it owns the deadline httpx used
        to provide (CR 1)."""
        inner = RecordingTransport()

        async def never_answers(host: str, port: int):
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        transport = GuardedTransport(inner, resolve=never_answers, resolve_timeout=0.01)

        with pytest.raises(httpx.ConnectTimeout):
            await _head(transport, "https://slow-dns.example.com/")

        assert inner.requests == []

    async def test_an_empty_answer_is_not_a_pass(self):
        """Zero addresses must refuse, not run the check loop zero times (CR 2)."""
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=resolver({"void.example.com": []}))

        with pytest.raises(httpx.ConnectError):
            await _head(transport, "https://void.example.com/")

        assert inner.requests == []

    @pytest.mark.parametrize(
        ("label", "answer"),
        [
            ("an exhausted generator", lambda: (value for value in [])),
            ("an empty iterator", lambda: iter(())),
            ("None", lambda: None),
        ],
    )
    async def test_an_empty_answer_of_any_shape_is_not_a_pass(self, label, answer):
        """Emptiness is judged on what the loop iterates, not on what came back (#316).

        ``not <generator>`` is ``False`` whatever it will yield, so testing the
        resolver's return value let an empty iterator through the same zero-trip
        loop CR 2 closed for the empty list. ``Resolver`` says ``Sequence``; the
        direction a guard fails in must not rest on the seam honouring that.
        """
        inner = RecordingTransport()

        async def resolve(host: str, port: int):
            return answer()

        transport = GuardedTransport(inner, resolve=resolve)

        with pytest.raises(httpx.ConnectError, match="no addresses"):
            await _head(transport, "https://void.example.com/")

        assert inner.requests == [], f"{label} let a request leave the host"

    @pytest.mark.parametrize(
        ("label", "answer"),
        [
            ("a hostname", "localhost"),
            ("an empty string", ""),
        ],
    )
    async def test_an_answer_that_is_not_an_address_is_not_a_pass(self, label, answer):
        """The arm #305 called unreachable failed *open* (#316).

        ``_containing`` answered ``None`` — "in no blocked range" — for anything
        it could not parse, so a resolver answering with a name rather than an
        address let the request through. Unreachable through ``getaddrinfo``,
        not through the seam.
        """
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=resolver({"named.example.com": [answer]}))

        with pytest.raises(httpx.ConnectError, match="not an address"):
            await _head(transport, "https://named.example.com/")

        assert inner.requests == [], f"{label} let a request leave the host"

    def test_the_resolve_cap_survives_one_libc_retry(self):
        """A cap equal to glibc's per-try timeout fails the resolve it should pass (#316).

        glibc's per-try timeout defaults to 5 s (``resolv.conf`` ``timeout:5``;
        co-watcher's is Tailscale MagicDNS with default options), so one dropped
        UDP packet makes a healthy resolve finish a little after 5 s, on libc's
        second try. The retry only *starts* at 5 s, so the cap needs room for a
        whole second try, not a hair over the first. Replicator's copy is 10 s
        for the same arithmetic (CannObserv/replicator#100).
        """
        glibc_per_try_seconds = 5.0

        assert RESOLVE_TIMEOUT >= 2 * glibc_per_try_seconds

    def test_a_refusal_is_not_an_httpx_error(self):
        """The routes' 'unreachable' branches must not swallow the refusal (#305)."""
        assert not issubclass(DestinationRefused, httpx.HTTPError)


class TestTransportPlumbing:
    async def test_closing_the_guard_closes_the_inner_transport(self):
        inner = RecordingTransport()
        transport = GuardedTransport(inner, resolve=never_resolves())

        await transport.aclose()

        assert inner.closed is True


class TestRealResolver:
    async def test_the_default_resolver_answers_from_the_running_loop(self):
        """``localhost`` comes from /etc/hosts — no network, and it is the
        address the guard exists to refuse."""
        addresses = await resolve_addresses("localhost", 80)

        assert addresses
        assert {"127.0.0.1", "::1"} & set(addresses)
