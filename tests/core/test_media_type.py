"""Tests for media-type dispatch helpers (#168 slice 2)."""

import co_core.pure.extract as co_extract

from src.core import media_type as watcher_media_type
from src.core.media_type import (
    AMBIGUOUS_MEDIA_TYPES,
    extension_media_type,
    media_type_essence_of,
    resolve_dispatch_essence,
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestDispatchIsCoCores:
    """#324: the dispatch rule lives in co-core; this module only re-exports it.

    cannobserv#486 D1: the issuer resolves the essence and puts it on the
    ``content.process`` command; the processor dispatches on it and never
    re-resolves (#366). So watcher must run co-core's function at the pinned
    version — a byte-identical copy drifts the first time co-core edits it, and
    the bump would not reach it. Identity, not equality of results.
    """

    def test_every_public_name_is_the_co_core_object(self):
        for name in (
            "AMBIGUOUS_MEDIA_TYPES",
            "extension_media_type",
            "media_type_essence_of",
            "resolve_dispatch_essence",
        ):
            assert getattr(watcher_media_type, name) is getattr(co_extract, name), name

    def test_ambiguous_set_is_the_shared_one(self):
        assert AMBIGUOUS_MEDIA_TYPES is co_extract.AMBIGUOUS_MEDIA_TYPES


class TestMediaTypeEssenceOf:
    def test_strips_params_and_lowercases(self):
        assert media_type_essence_of("Text/HTML; charset=utf-8") == "text/html"

    def test_plain_type_unchanged(self):
        assert media_type_essence_of("application/pdf") == "application/pdf"

    def test_none_and_empty(self):
        assert media_type_essence_of(None) is None
        assert media_type_essence_of("") is None
        assert media_type_essence_of("   ") is None


class TestExtensionMediaType:
    def test_known_extensions(self):
        assert extension_media_type("https://x.gov/a.pdf") == "application/pdf"
        assert extension_media_type("https://x.gov/data.csv") == "text/csv"
        assert extension_media_type("https://x.gov/sheet.xlsx") == _XLSX
        assert extension_media_type("https://x.gov/page.html") == "text/html"

    def test_feed_extensions(self):
        # #366: co-core 0.32.0 (cannobserv#523) adds the XML essences Processor's
        # XmlExtractor takes; watcher must resolve them too, as Processor never re-resolves.
        assert extension_media_type("https://x.gov/feed.xml") == "application/xml"
        assert extension_media_type("https://x.gov/feed.rss") == "application/rss+xml"
        assert extension_media_type("https://x.gov/feed.atom") == "application/atom+xml"

    def test_query_string_ignored(self):
        assert extension_media_type("https://x.gov/a.pdf?v=2") == "application/pdf"

    def test_no_extension_or_url(self):
        assert extension_media_type("https://x.gov/page") is None
        assert extension_media_type("") is None
        assert extension_media_type(None) is None


class TestResolveDispatchEssence:
    def test_informative_header_wins(self):
        assert (
            resolve_dispatch_essence("application/pdf", "https://x.gov/a.html") == "application/pdf"
        )

    def test_ambiguous_header_falls_back_to_extension(self):
        assert (
            resolve_dispatch_essence("application/octet-stream", "https://x.gov/a.pdf")
            == "application/pdf"
        )
        assert resolve_dispatch_essence("text/plain", "https://x.gov/data.csv") == "text/csv"

    def test_ambiguous_header_falls_back_to_feed_extension(self):
        # #366: before 0.32.0 these resolved to the header's essence and Processor
        # extracted them as HTML.
        assert (
            resolve_dispatch_essence("application/octet-stream", "https://x.gov/feed.xml")
            == "application/xml"
        )
        assert (
            resolve_dispatch_essence("binary/octet-stream", "https://x.gov/feed.rss")
            == "application/rss+xml"
        )
        assert resolve_dispatch_essence("text/plain", "https://x.gov/feed.atom") == (
            "application/atom+xml"
        )
        assert resolve_dispatch_essence(None, "https://x.gov/feed.xml") == "application/xml"

    def test_informative_xml_header_wins_over_feed_extension(self):
        # Tailscale's feed: served as text/xml, so it resolves the same on 0.19.7 and 0.32.0.
        assert resolve_dispatch_essence("text/xml", "https://x.gov/feed.rss") == "text/xml"

    def test_missing_header_uses_extension(self):
        assert resolve_dispatch_essence(None, "https://x.gov/a.pdf") == "application/pdf"

    def test_ambiguous_header_no_extension_returns_header_essence(self):
        # Resolves to the ambiguous essence; the processor maps it to the HTML fallback.
        assert (
            resolve_dispatch_essence("application/octet-stream", "https://x.gov/page")
            == "application/octet-stream"
        )

    def test_nothing_informative_returns_none(self):
        assert resolve_dispatch_essence(None, "https://x.gov/page") is None
