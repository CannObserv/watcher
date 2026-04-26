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
    def test_empty_passthrough(self):
        assert normalize_html("") == ""

    def test_whitespace_only_passthrough(self):
        assert normalize_html("   \n\t  ") == "   \n\t  "

    def test_single_line_html_gets_block_wrapping(self):
        """Block-level elements on one line each get their own line — the whole point of #118."""
        src = "<div><p>One</p><p>Two</p><p>Three</p></div>"
        out = normalize_html(src)
        # Each <p> should be on its own line; the original input was one line.
        assert out.count("\n") >= 3
        # Content preserved
        assert "One" in out and "Two" in out and "Three" in out

    def test_inline_elements_stay_inline(self):
        """Inline elements like <b>, <em>, <a> shouldn't be split onto their own lines."""
        src = "<p>Hello <b>bold</b> and <em>italic</em> text</p>"
        out = normalize_html(src)
        # The <b>bold</b> stays on the same line as "Hello".
        assert "Hello <b>bold</b>" in out

    def test_idempotent(self):
        """Re-normalizing a pretty-printed page yields the same output."""
        src = "<div><p>Hello</p><p>World</p></div>"
        once = normalize_html(src)
        twice = normalize_html(once)
        assert once == twice

    def test_strips_html_comments(self):
        """Comments are noise that often changes between snapshots — strip them."""
        src = "<div><!-- generated 2026-04-26 --><p>Content</p></div>"
        out = normalize_html(src)
        assert "generated" not in out
        assert "<!--" not in out
        assert "Content" in out

    def test_tolerates_malformed_html(self):
        """html5lib is forgiving — unclosed tags get repaired without crashing."""
        out = normalize_html("<p>unclosed <b>bold")
        assert "unclosed" in out
        assert "</b>" in out  # auto-closed

    def test_round_trip_identical_inputs_produce_identical_output(self):
        """Determinism guarantee for the #118 'identical pages → has_changes=False' acceptance."""
        src = "<html><body><p>Same content</p></body></html>"
        a = normalize_html(src)
        b = normalize_html(src)
        assert a == b

    def test_crlf_input_produces_lf_output(self):
        """Output uses LF newlines so it composes cleanly with normalize_text downstream."""
        src = "<div>\r\n<p>x</p>\r\n</div>"
        out = normalize_html(src)
        assert "\r" not in out
