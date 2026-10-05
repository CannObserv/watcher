"""Tests for the change diff's pure half (#222): compute, and who asks for one.

The diff target is the canonical extracted text (cannobserv#486). Loading it
back is ``diff_loader``'s job (``test_diff_loader.py``).
"""

import pytest

from src.api.schemas.content_config import ContentOptions
from src.core.notifications.diff import MAX_SEGMENT_CHARS, compute_unified_diff, diff_requested

PREVIOUS = b"Hours\nMon-Fri 9-5\nContact: a@example.com"
CURRENT = b"Hours\nMon-Fri 9-6\nContact: a@example.com"


class TestComputeUnifiedDiff:
    def test_line_diff_over_the_canonical_text(self):
        out = compute_unified_diff(PREVIOUS, CURRENT)
        lines = out.split("\n")
        assert lines[0].startswith("--- ")
        assert lines[1].startswith("+++ ")
        assert lines[2].startswith("@@")
        assert "-Mon-Fri 9-5" in lines
        assert "+Mon-Fri 9-6" in lines
        assert " Hours" in lines

    def test_no_blank_lines_between_content_lines(self):
        # The render helper drops empty lines; a diff built with lineterm=""
        # never relies on that to look right.
        out = compute_unified_diff(PREVIOUS, CURRENT)
        assert "" not in out.split("\n")

    def test_a_one_line_page_diffs_by_sentence(self):
        """Every live item extracts to one whitespace-collapsed line (2026-10-05:
        8–17 KB each); a line diff would print the page twice. Sentences are the
        unit instead, so an edit shows the sentence it touched."""
        before = b"Board meets monthly. Two members form a quorum. Minutes are posted."
        after = b"Board meets monthly. Three members form a quorum. Minutes are posted."
        lines = compute_unified_diff(before, after).split("\n")
        assert "-Two members form a quorum." in lines
        assert "+Three members form a quorum." in lines
        assert " Board meets monthly." in lines
        assert not any("Minutes" in line and line[0] in "+-" for line in lines)

    def test_a_long_unpunctuated_run_is_wrapped(self):
        words = b" ".join(b"word%d" % i for i in range(400))
        out = compute_unified_diff(words, words + b" tail")
        assert max(len(line) for line in out.split("\n")) <= MAX_SEGMENT_CHARS + 1

    def test_undecodable_bytes_do_not_raise(self):
        assert "+" in compute_unified_diff(b"caf\xc3", b"caf\xc3\xa9")


class TestDiffRequested:
    def test_snippet_is_on_by_default(self):
        assert diff_requested(ContentOptions()) is True

    def test_off_when_both_toggles_off(self):
        assert diff_requested(ContentOptions(include_diff_snippet=False)) is False

    def test_full_alone_requests_it(self):
        assert diff_requested(ContentOptions(include_diff_snippet=False, include_diff_full=True))

    @pytest.mark.parametrize("var", ["diff_snippet", "diff_full"])
    def test_a_custom_body_naming_a_diff_variable_requests_it(self, var):
        opts = ContentOptions(include_diff_snippet=False, body_template=f"x {{{{ {var} }}}}")
        assert diff_requested(opts) is True

    def test_a_custom_body_without_one_does_not(self):
        # Toggles do not apply under a custom body; only its variables do.
        opts = ContentOptions(include_diff_snippet=True, body_template="{{ item_url }}")
        assert diff_requested(opts) is False
