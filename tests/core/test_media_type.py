"""Tests for media-type dispatch helpers (#168 slice 2)."""

from src.core.media_type import (
    extension_media_type,
    extraction_overrides_for_essence,
    media_type_essence_of,
    resolve_dispatch_essence,
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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

    def test_missing_header_uses_extension(self):
        assert resolve_dispatch_essence(None, "https://x.gov/a.pdf") == "application/pdf"

    def test_ambiguous_header_no_extension_returns_header_essence(self):
        # Resolves to the ambiguous essence -> registry maps it to the HTML fallback.
        assert (
            resolve_dispatch_essence("application/octet-stream", "https://x.gov/page")
            == "application/octet-stream"
        )

    def test_nothing_informative_returns_none(self):
        assert resolve_dispatch_essence(None, "https://x.gov/page") is None


class TestExtractionOverrides:
    def test_csv_and_xlsx_set_content_type(self):
        assert extraction_overrides_for_essence("text/csv") == {"content_type": "csv"}
        assert extraction_overrides_for_essence(_XLSX) == {"content_type": "xlsx"}

    def test_html_pdf_and_unknown_have_no_overrides(self):
        assert extraction_overrides_for_essence("text/html") == {}
        assert extraction_overrides_for_essence("application/pdf") == {}
        assert extraction_overrides_for_essence(None) == {}
