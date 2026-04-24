"""Tests for the normalization stage."""

from src.core.diff.normalize import normalize_html, normalize_text


class TestNormalizeText:
    def test_passthrough_lf(self):
        assert normalize_text("a\nb\nc") == "a\nb\nc"

    def test_crlf_to_lf(self):
        assert normalize_text("a\r\nb\r\nc") == "a\nb\nc"

    def test_cr_to_lf(self):
        assert normalize_text("a\rb\rc") == "a\nb\nc"

    def test_strip_trailing_whitespace(self):
        assert normalize_text("a   \nb\t\nc") == "a\nb\nc"

    def test_preserves_leading_whitespace(self):
        assert normalize_text("  indent\n\tindent") == "  indent\n\tindent"

    def test_preserves_blank_lines(self):
        assert normalize_text("a\n\nb") == "a\n\nb"

    def test_empty(self):
        assert normalize_text("") == ""


class TestNormalizeHtml:
    def test_phase_a_is_passthrough(self):
        """Phase A stub: returns input unchanged. Phase B (xmldiff) replaces this."""
        src = "<div>  <p>hi</p>  </div>"
        assert normalize_html(src) == src

    def test_empty(self):
        assert normalize_html("") == ""
