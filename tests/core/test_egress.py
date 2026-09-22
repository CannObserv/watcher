"""Unit tests for the probe destination guard (#305)."""

import asyncio

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
