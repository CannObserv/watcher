"""Tests for compute_unified_diff."""

from src.core.diff.textual import compute_unified_diff


class TestComputeUnifiedDiff:
    def test_identical_text_has_no_changes(self):
        r = compute_unified_diff("hello\nworld\n", "hello\nworld\n")
        assert r.has_changes is False
        assert r.unified_diff == ""
        assert r.added == 0
        assert r.removed == 0

    def test_simple_modification(self):
        r = compute_unified_diff("hello\nworld\n", "hello\nplanet\n")
        assert r.has_changes is True
        assert "-world" in r.unified_diff
        assert "+planet" in r.unified_diff
        assert r.added == 1
        assert r.removed == 1

    def test_pure_addition(self):
        r = compute_unified_diff("a\n", "a\nb\n")
        assert r.has_changes is True
        assert r.added == 1
        assert r.removed == 0

    def test_pure_deletion(self):
        r = compute_unified_diff("a\nb\n", "a\n")
        assert r.has_changes is True
        assert r.added == 0
        assert r.removed == 1

    def test_header_uses_identical_filename(self):
        """Both sides use the same filename label 'content'.

        Identical from/to filenames prevent diff2html from rendering a
        spurious 'RENAMED' badge in the file header.
        """
        r = compute_unified_diff("a\n", "b\n")
        assert r.unified_diff.startswith("--- content\n+++ content\n")

    def test_whitespace_only_change_is_normalized_away(self):
        """Trailing whitespace differences should not show as changes."""
        r = compute_unified_diff("hello   \nworld\n", "hello\nworld\n")
        assert r.has_changes is False

    def test_crlf_vs_lf_is_normalized_away(self):
        r = compute_unified_diff("a\r\nb\r\n", "a\nb\n")
        assert r.has_changes is False

    def test_empty_both(self):
        r = compute_unified_diff("", "")
        assert r.has_changes is False
        assert r.unified_diff == ""

    def test_empty_previous(self):
        r = compute_unified_diff("", "new line\n")
        assert r.has_changes is True
        assert r.added == 1

    def test_context_lines_included(self):
        """Default context lines means surrounding unchanged lines appear in output."""
        prev = "a\nb\nc\nd\ne\n"
        curr = "a\nb\nX\nd\ne\n"
        r = compute_unified_diff(prev, curr, context=3)
        # Context should include 'b' before and 'd' after the change.
        assert " b" in r.unified_diff
        assert " d" in r.unified_diff

    def test_custom_context(self):
        r0 = compute_unified_diff("a\nb\nc\nX\ne\nf\ng\n", "a\nb\nc\nY\ne\nf\ng\n", context=0)
        r3 = compute_unified_diff("a\nb\nc\nX\ne\nf\ng\n", "a\nb\nc\nY\ne\nf\ng\n", context=3)
        # More context ⇒ more total lines in the diff body.
        assert len(r3.unified_diff.splitlines()) > len(r0.unified_diff.splitlines())
