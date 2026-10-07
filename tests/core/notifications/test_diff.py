"""Tests for the change diff's pure half (#222, #349): compute, and who asks for one.

The diff target is the canonical extracted text (cannobserv#486). Loading it
back is ``diff_loader``'s job (``test_diff_loader.py``). #349 replaced the
sentence/fixed-width segmentation with a word diff aligned on content-defined
segments; ``TestWslcbRegression`` is the change that showed why.
"""

import difflib
import random
import re
import textwrap
import time
from pathlib import Path

import pytest

from src.api.schemas.content_config import ContentOptions
from src.core.notifications import diff as diff_mod
from src.core.notifications.diff import (
    CONTEXT_WORDS,
    WRAP_WIDTH,
    ChangeDiff,
    compute_change_diff,
    diff_requested,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "diff"
WSLCB_PREVIOUS = (FIXTURES / "wslcb-2026-10-06-previous.txt").read_bytes()
WSLCB_CURRENT = (FIXTURES / "wslcb-2026-10-06-current.txt").read_bytes()


def _changed(diff: ChangeDiff) -> list[str]:
    return [line for hunk in diff.hunks for line in hunk if line[:1] in "+-"]


def _words(n: int, prefix: str = "w") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


class TestWslcbRegression:
    """2026-10-06, WSLCB - Meeting Schedule: a three-word edit early in an
    11.5 KB run with no sentence end. The fixture is both stored texts
    (``sha256:b8f6d0f1…`` → ``sha256:2d98c2c4…``) cut at the same offset, from
    "September 2026" to the end — still one unpunctuated run."""

    def test_the_fixture_reproduces_the_realignment(self):
        """Under #222's segmentation (sentences, then a fixed 400-character
        wrap) the 33-character insertion shifts every later wrap boundary."""

        def old_segments(text: bytes) -> list[str]:
            out = []
            for sentence in re.split(r"(?<=[.!?])\s+", text.decode()):
                out.extend(textwrap.wrap(sentence, 400, break_on_hyphens=False) or [sentence])
            return out

        before, after = old_segments(WSLCB_PREVIOUS), old_segments(WSLCB_CURRENT)
        changed = [
            line
            for line in difflib.unified_diff(before, after, lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("---", "+++"))
        ]
        assert len(changed) >= 20

    def test_the_change_is_the_three_words_not_the_run(self):
        diff = compute_change_diff(WSLCB_PREVIOUS, WSLCB_CURRENT)
        assert len(diff.hunks) == 1
        assert _changed(diff) == [
            "- To watch the 10/6/26 meeting on TVW",
            "+ Meeting recordings: 10/6/26 MS Teams Recording 10/6/26 TVW Recording",
        ]

    def test_the_hunk_shows_where_on_the_page_it_is(self):
        (hunk,) = compute_change_diff(WSLCB_PREVIOUS, WSLCB_CURRENT).hunks
        assert hunk[0] == "  … October 6, 10 - 11, Board Caucus Agenda"
        assert hunk[-1] == "  Wednesday, October 7, 10 - 11, Board Meeting …"

    def test_the_diff_is_on_the_order_of_the_change(self):
        diff = compute_change_diff(WSLCB_PREVIOUS, WSLCB_CURRENT)
        assert len("\n".join(diff.hunks[0]).encode()) < 400


class TestWordDiff:
    def test_an_insertion_early_in_an_unpunctuated_run_marks_only_itself(self):
        run = _words(2000)
        after = run[:10] + ["newly", "inserted", "words"] + run[10:]
        diff = compute_change_diff(" ".join(run).encode(), " ".join(after).encode())
        assert _changed(diff) == ["+ newly inserted words"]

    def test_a_deletion_has_no_plus_line(self):
        before = _words(100)
        after = before[:50] + before[53:]
        diff = compute_change_diff(" ".join(before).encode(), " ".join(after).encode())
        assert _changed(diff) == ["- w50 w51 w52"]

    def test_changes_a_few_words_apart_fold_into_one_pair(self):
        before = _words(60)
        after = list(before)
        after[30], after[33] = "X", "Y"
        diff = compute_change_diff(" ".join(before).encode(), " ".join(after).encode())
        assert _changed(diff) == ["- w30 w31 w32 w33", "+ X w31 w32 Y"]

    def test_folding_never_crosses_a_chunk_boundary(self):
        """Folding rewords a phrase; an unchanged line between two edited ones
        stays a context line, as in a line diff."""
        (hunk,) = compute_change_diff(b"a\nb\nc", b"x\nb\ny").hunks
        assert hunk == ("- a", "+ x", "  b", "- c", "+ y")

    def test_changes_within_twice_the_context_share_a_hunk(self):
        before = _words(200)
        after = list(before)
        after[50], after[60] = "X", "Y"
        diff = compute_change_diff(" ".join(before).encode(), " ".join(after).encode())
        assert len(diff.hunks) == 1
        assert "  w51 w52 w53 w54 w55 w56 w57 w58 w59" in diff.hunks[0]

    def test_distant_changes_are_separate_hunks(self):
        before = _words(400)
        after = list(before)
        after[50], after[300] = "X", "Y"
        diff = compute_change_diff(" ".join(before).encode(), " ".join(after).encode())
        assert len(diff.hunks) == 2

    def test_context_is_bounded_and_marks_its_cut(self):
        before = _words(100)
        after = list(before)
        after[50] = "X"
        (hunk,) = compute_change_diff(" ".join(before).encode(), " ".join(after).encode()).hunks
        assert hunk == (
            "  … " + " ".join(before[50 - CONTEXT_WORDS : 50]),
            "- w50",
            "+ X",
            "  " + " ".join(before[51 : 51 + CONTEXT_WORDS]) + " …",
        )

    def test_no_ellipsis_at_the_edges_of_the_text(self):
        (hunk,) = compute_change_diff(b"alpha beta", b"alpha gamma").hunks
        assert hunk == ("  alpha", "- beta", "+ gamma")

    def test_chunk_boundaries_are_line_breaks(self):
        before = b"Hours\nMon-Fri 9-5\nContact a@example.com"
        after = b"Hours\nMon-Fri 9-6\nContact a@example.com"
        (hunk,) = compute_change_diff(before, after).hunks
        assert hunk == ("  Hours", "  Mon-Fri", "- 9-5", "+ 9-6", "  Contact a@example.com")

    def test_context_counts_words_not_chunk_boundaries(self):
        """CR 3: a ``\\n`` is not a word; on a line-structured page it must not
        eat into the context."""
        before = b"a1 a2 a3\nb1 b2 b3\nc1 c2 c3\nd1 d2 X d4 d5 d6\ne1 e2 e3\nf1 f2 f3"
        (hunk,) = compute_change_diff(before, before.replace(b"X", b"Y")).hunks
        assert hunk == (
            "  b1 b2 b3",
            "  c1 c2 c3",
            "  d1 d2",
            "- X",
            "+ Y",
            "  d4 d5 d6",
            "  e1 e2 e3",
            "  f1 f2 …",
        )

    def test_changes_within_twice_the_context_words_share_a_hunk_across_lines(self):
        lines = [" ".join(f"r{row}w{i}" for i in range(3)) for row in range(20)]
        edited = list(lines)
        edited[5], edited[10] = "X r5w1 r5w2", "Y r10w1 r10w2"
        diff = compute_change_diff("\n".join(lines).encode(), "\n".join(edited).encode())
        assert len(diff.hunks) == 1

    def test_a_change_spanning_a_chunk_boundary_keeps_its_lines(self):
        diff = compute_change_diff(b"a\nb c\nd", b"a\nx\ny\nd")
        assert _changed(diff) == ["- b c", "+ x", "+ y"]

    def test_undecodable_bytes_do_not_raise(self):
        assert _changed(compute_change_diff(b"caf\xc3", b"caf\xc3\xa9"))


class TestWrap:
    def test_long_lines_wrap_and_keep_their_prefix(self):
        inserted = " ".join(_words(60, "inserted"))
        diff = compute_change_diff(b"start end", f"start {inserted} end".encode())
        plus = [line for line in diff.hunks[0] if line.startswith("+")]
        assert len(plus) > 1
        assert all(len(line) <= WRAP_WIDTH + 2 for line in plus)
        assert " ".join(line[2:] for line in plus) == inserted

    def test_one_word_longer_than_the_width_stays_whole(self):
        url = "https://example.com/" + "a" * 120
        diff = compute_change_diff(b"see here", f"see {url}".encode())
        assert f"+ {url}" in diff.hunks[0]


class TestWhitespaceOnly:
    def test_collapsed_whitespace_is_no_change(self):
        assert compute_change_diff(b"Board meets.  Quorum.", b"Board meets. Quorum.").hunks == ()

    def test_a_moved_chunk_boundary_alone_is_no_change(self):
        assert compute_change_diff(b"a b\nc d", b"a\nb c d").hunks == ()


class TestCost:
    """A flat word diff is quadratic in pure Python, holding the GIL in the one
    process that serves everything: 60 s+ for one edit in a 150k-word page."""

    def test_a_large_page_with_one_edit_diffs_quickly(self):
        rng = random.Random(349)
        vocab = [f"w{i}" for i in range(3000)] + ["the", "of", "and"] * 300
        page = [rng.choice(vocab) for _ in range(100_000)]
        edited = page[:50_000] + ["inserted"] * 5 + page[50_000:]
        started = time.perf_counter()
        diff = compute_change_diff(" ".join(page).encode(), " ".join(edited).encode())
        assert time.perf_counter() - started < 5
        assert _changed(diff) == ["+ inserted inserted inserted inserted inserted"]

    def test_a_large_page_rewritten_diffs_quickly(self):
        rng = random.Random(349)
        vocab = [f"w{i}" for i in range(3000)]
        before = " ".join(rng.choice(vocab) for _ in range(100_000))
        after = " ".join(rng.choice(vocab) for _ in range(100_000))
        started = time.perf_counter()
        assert compute_change_diff(before.encode(), after.encode()).hunks
        assert time.perf_counter() - started < 5

    ROW = "Tuesday, October 6, 10 - 11, Board Caucus Agenda Meeting Recordings: MS Teams TVW"

    def test_a_repetitive_page_with_one_edit_diffs_quickly(self):
        """CR 1: a schedule's rows make identical segments — SequenceMatcher's
        worst case without autojunk (69 s for one edit in 10 000 rows)."""
        words = " ".join([self.ROW] * 10_000).split()
        edited = [*words[:80_000], "inserted", *words[80_000:]]
        started = time.perf_counter()
        diff = compute_change_diff(" ".join(words).encode(), " ".join(edited).encode())
        assert time.perf_counter() - started < 5
        assert _changed(diff) == ["+ inserted"]

    def test_a_repetitive_page_with_scattered_edits_diffs_quickly(self):
        words = " ".join([self.ROW] * 10_000).split()
        edited = list(words)
        for at in range(1_000, len(words), 16_000):
            edited[at] = "X"
        started = time.perf_counter()
        assert compute_change_diff(" ".join(words).encode(), " ".join(edited).encode()).hunks
        assert time.perf_counter() - started < 5

    def test_a_small_vocabulary_rewrite_diffs_quickly(self):
        rng = random.Random(349)
        vocab = ["Board", "Caucus", "Agenda", "Meeting", "TVW", "Tuesday,", "10", "-", "11,"]
        before = " ".join(rng.choice(vocab) for _ in range(100_000))
        after = " ".join(rng.choice(vocab) for _ in range(100_000))
        started = time.perf_counter()
        assert compute_change_diff(before.encode(), after.encode()).hunks
        assert time.perf_counter() - started < 5

    def test_a_block_over_the_refine_budget_is_shown_whole(self, monkeypatch):
        # Two edits, so the trimmed middle still holds changed segments.
        words = _words(200)
        edited = [*words[:50], "X", *words[51:150], "Y", *words[151:]]
        before, after = " ".join(words).encode(), " ".join(edited).encode()
        assert _changed(compute_change_diff(before, after)) == ["- w50", "+ X", "- w150", "+ Y"]
        monkeypatch.setattr(diff_mod, "REFINE_MAX_TOKENS", 1)
        minus = [line for line in _changed(compute_change_diff(before, after)) if line[0] == "-"]
        assert "w50" in " ".join(minus).split()
        assert len(" ".join(minus).split()) > 4


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
