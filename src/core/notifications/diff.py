"""The change diff (#222, #349): what it is and how it is computed — pure.

The diff target is the canonical extracted text (cannobserv#486) — the exact
bytes a ``ChangeRevision.content_fingerprint`` hashes. This module holds the
value the renderer consumes (``ChangeDiff``), the computation and the question
of whether a recipient wants one. It touches no database, store or bus:
loading the two texts is ``diff_loader``'s job, so the renderer and the preview
can import this without the I/O stack (CR 2).

**The unit is the word (#349).** Every live item extracts to one long line, and
a schedule or list page has almost no sentence ends, so #222's sentence split
fell back to a fixed-width wrap whose boundaries all shifted after an edit —
a three-word change on 2026-10-06 rendered as 11 KB of ``-``/``+``. Here the
text is words plus ``\\n`` (a chunk boundary), and a diff reads as the changed
words with ``CONTEXT_WORDS`` either side.

**Aligned in two levels.** A flat word ``difflib`` without autojunk is
quadratic, and pure Python holds the GIL in the one process that serves
everything (63 s for one edit in a 150k-word page). The shared prefix and
suffix are trimmed first. Level 1 aligns content-defined segments of the rest:
one ends after a token whose own hash says so, so an edit moves no natural
boundary but its own. A run of ``MAX_SEGMENT_TOKENS`` with no natural boundary
is cut by position, and those cuts shift up to the run's next natural boundary
— bounded realignment, never the page's (CR 5). Level 2 refines changed
blocks word by word while their summed work (``len(a) × len(b)``) stays within
``REFINE_BUDGET``; a block past it is shown whole, bounded by the renderer's
byte cap (CR 7). On a repetitive page (a schedule's identical rows), two edits
far enough apart show the stretch between them whole (CR 1).
"""

import difflib
import re
import textwrap
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

from src.api.schemas.content_config import ContentOptions

# The template variables that carry a diff; a custom body naming neither has
# no use for one.
DIFF_TEMPLATE_VARIABLES = ("diff_snippet", "diff_full")

#: Unchanged words shown either side of a change.
CONTEXT_WORDS = 8
#: Changes this few unchanged words apart read as one replacement.
FOLD_WORDS = 3
#: Text columns per rendered line, after the two-character prefix.
WRAP_WIDTH = 72
#: A segment ends after a token whose crc32 is 0 modulo this — about 16 words.
SEGMENT_MODULUS = 16
#: A run with no such token is still cut, so no segment outgrows this.
MAX_SEGMENT_TOKENS = 64
#: Word-level work, summed over every refined block (``len(a) × len(b)``),
#: one diff may spend: about one 4 000-word block of a small vocabulary, ~1 s
#: (CR 7). A block that would overrun it is shown whole, not refined.
REFINE_BUDGET = 4000 * 4000

_TOKEN = re.compile(r"\n|[^\s]+")
_NEWLINE = "\n"

#: One hunk: rendered lines, each prefixed ``"- "``, ``"+ "`` or ``"  "``.
Hunk = tuple[str, ...]
# A change: (i1, i2, j1, j2) — previous[i1:i2] became current[j1:j2].
_Span = tuple[int, int, int, int]


@dataclass(frozen=True)
class ChangeDiff:
    """One event's diff, or why there is none.

    ``hunks`` are the changes in page order, each with its context;
    ``unavailable`` is set exactly when no diff could be produced.
    """

    hunks: tuple[Hunk, ...] = ()
    unavailable: str | None = None


def diff_requested(options: ContentOptions) -> bool:
    """Whether a recipient with these options would show a diff.

    A custom body ignores the toggles (``build_body``), so it asks for one only
    by naming a diff variable.
    """
    if options.body_template:
        return any(name in options.body_template for name in DIFF_TEMPLATE_VARIABLES)
    return options.include_diff_snippet or options.include_diff_full


def compute_change_diff(previous: bytes, current: bytes) -> ChangeDiff:
    """The word diff of two canonical texts. Pure CPU — callers off the loop
    use a thread.

    No hunks means no difference a reader could see: the texts differ only in
    whitespace (``"\\n"`` included).
    """
    before = _TOKEN.findall(previous.decode("utf-8", errors="replace"))
    after = _TOKEN.findall(current.decode("utf-8", errors="replace"))
    spans = [
        span for span in _changed_spans(before, after) if not _whitespace_only(span, before, after)
    ]
    groups = _group(_fold(spans, before), before, gap=2 * CONTEXT_WORDS)
    return ChangeDiff(hunks=tuple(_render_hunk(group, before, after) for group in groups))


def _segments(tokens: list[str]) -> list[tuple[str, ...]]:
    """Content-defined segments: a natural boundary depends only on the token
    before it. A positional cut every ``MAX_SEGMENT_TOKENS`` of a run with none
    can shift after an edit, but only until the run's next natural boundary."""
    out: list[tuple[str, ...]] = []
    start = 0
    for index, token in enumerate(tokens):
        if (
            token == _NEWLINE
            or zlib.crc32(token.encode()) % SEGMENT_MODULUS == 0
            or index + 1 - start >= MAX_SEGMENT_TOKENS
        ):
            out.append(tuple(tokens[start : index + 1]))
            start = index + 1
    if start < len(tokens):
        out.append(tuple(tokens[start:]))
    return out


def _offsets(segments: list[tuple[str, ...]]) -> list[int]:
    offsets = [0]
    for segment in segments:
        offsets.append(offsets[-1] + len(segment))
    return offsets


def _common_ends(before: list[str], after: list[str]) -> tuple[int, int]:
    """Lengths of the shared token prefix and suffix, never overlapping."""
    limit = min(len(before), len(after))
    head = 0
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while tail < limit - head and before[-1 - tail] == after[-1 - tail]:
        tail += 1
    return head, tail


def _changed_spans(before: list[str], after: list[str]) -> Iterator[_Span]:
    """Every non-equal opcode, in token positions: segments first, then words.

    The shared prefix and suffix are trimmed first (CR 1): a schedule's rows
    make identical segments, and identical items are ``SequenceMatcher``'s
    worst case — 69 s for one edit in 10 000 rows. Trimmed, one edit costs a
    scan however repetitive the page. What remains is aligned with autojunk
    on, so a segment that recurs throughout it cannot anchor the alignment:
    a repetitive stretch between two edits becomes one changed block.

    Refining costs about ``len(a) × len(b)`` a block, so the budget is for the
    whole diff, not per block (CR 7): capped per block only, 42 blocks of a
    small vocabulary took 35 s. Blocks past ``REFINE_BUDGET`` are shown whole.
    """
    head, tail = _common_ends(before, after)
    middle_before = before[head : len(before) - tail]
    middle_after = after[head : len(after) - tail]
    seg_before, seg_after = _segments(middle_before), _segments(middle_after)
    at_before = [head + offset for offset in _offsets(seg_before)]
    at_after = [head + offset for offset in _offsets(seg_after)]
    matcher = difflib.SequenceMatcher(None, seg_before, seg_after)
    budget = REFINE_BUDGET
    for tag, s1, s2, t1, t2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        i1, i2, j1, j2 = at_before[s1], at_before[s2], at_after[t1], at_after[t2]
        work = (i2 - i1) * (j2 - j1)
        if work > budget:
            yield i1, i2, j1, j2
            continue
        budget -= work
        words = difflib.SequenceMatcher(None, before[i1:i2], after[j1:j2], autojunk=False)
        for wtag, a1, a2, b1, b2 in words.get_opcodes():
            if wtag != "equal":
                yield i1 + a1, i1 + a2, j1 + b1, j1 + b2


def _whitespace_only(span: _Span, before: list[str], after: list[str]) -> bool:
    i1, i2, j1, j2 = span
    return all(token == _NEWLINE for token in (*before[i1:i2], *after[j1:j2]))


def _fold(spans: list[_Span], before: list[str]) -> list[_Span]:
    """Merge changes at most ``FOLD_WORDS`` apart on one line, the words
    between included: a reworded phrase reads as one replacement. An
    unchanged line between two changes stays context."""
    out: list[_Span] = []
    for span in spans:
        gap = before[out[-1][1] : span[0]] if out else []
        if out and len(gap) <= FOLD_WORDS and _NEWLINE not in gap:
            out[-1] = (out[-1][0], span[1], out[-1][2], span[3])
        else:
            out.append(span)
    return out


def _words_in(tokens: list[str]) -> int:
    return sum(1 for token in tokens if token != _NEWLINE)


def _group(spans: list[_Span], before: list[str], *, gap: int) -> list[list[_Span]]:
    """Changes close enough that their contexts would overlap share a hunk.

    ``gap`` counts words: a chunk boundary is not one (CR 3).
    """
    groups: list[list[_Span]] = []
    for span in spans:
        if groups and _words_in(before[groups[-1][-1][1] : span[0]]) <= gap:
            groups[-1].append(span)
        else:
            groups.append([span])
    return groups


def _render_hunk(group: list[_Span], before: list[str], after: list[str]) -> Hunk:
    """Context, then each change as ``-``/``+`` lines, then context."""
    first, last = group[0], group[-1]
    start = _context_start(before, first[0])
    lead = before[start : first[0]]
    lines = _lines("  ", lead, cut_before=start > 0 and before[start - 1] != _NEWLINE)
    for index, (i1, i2, j1, j2) in enumerate(group):
        lines += _lines("- ", before[i1:i2]) + _lines("+ ", after[j1:j2])
        if index + 1 < len(group):
            lines += _lines("  ", before[i2 : group[index + 1][0]])
    end = _context_end(before, last[1])
    trail = before[last[1] : end]
    lines += _lines("  ", trail, cut_after=end < len(before) and before[end] != _NEWLINE)
    return tuple(lines)


def _context_start(tokens: list[str], at: int) -> int:
    """Where ``CONTEXT_WORDS`` words before ``at`` begin (CR 3: ``\\n`` is no word)."""
    words = 0
    while at > 0 and words < CONTEXT_WORDS:
        at -= 1
        words += tokens[at] != _NEWLINE
    return at


def _context_end(tokens: list[str], at: int) -> int:
    """Where ``CONTEXT_WORDS`` words after ``at`` end (CR 3: ``\\n`` is no word)."""
    words = 0
    while at < len(tokens) and words < CONTEXT_WORDS:
        words += tokens[at] != _NEWLINE
        at += 1
    return at


def _lines(
    prefix: str, tokens: list[str], *, cut_before: bool = False, cut_after: bool = False
) -> list[str]:
    """Tokens as wrapped, prefixed lines: one per chunk line, none for an empty one.

    ``…`` marks context cut short mid-line.
    """
    pieces = " ".join(tokens).split(_NEWLINE)
    if cut_before:
        pieces[0] = f"… {pieces[0]}"
    if cut_after:
        pieces[-1] = f"{pieces[-1]} …"
    out: list[str] = []
    for piece in pieces:
        text = piece.strip()
        if text and text != "…":
            for line in textwrap.wrap(
                text, WRAP_WIDTH, break_long_words=False, break_on_hyphens=False
            ):
                out.append(f"{prefix}{line}")
    return out
