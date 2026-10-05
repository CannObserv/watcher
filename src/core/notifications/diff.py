"""The change diff (#222): what it is and how it is computed — pure.

The diff target is the canonical extracted text (cannobserv#486) — the exact
bytes a ``ChangeRevision.content_fingerprint`` hashes. This module holds the
value the renderer consumes (``ChangeDiff``), the computation (``difflib`` by
sentence) and the question of whether a recipient wants one. It touches no
database, store or bus: loading the two texts is ``diff_loader``'s job, so the
renderer and the preview can import this without the I/O stack (CR 2).
"""

import difflib
import re
import textwrap
from dataclasses import dataclass

from src.api.schemas.content_config import ContentOptions

# The template variables that carry a diff; a custom body naming neither has
# no use for one.
DIFF_TEMPLATE_VARIABLES = ("diff_snippet", "diff_full")

# The diff's unit is a sentence, not a line. A chunk's text is one
# whitespace-collapsed line, and a spec usually selects one element — every
# live item extracted to a single 8–17 KB line on 2026-10-05 — so a line diff
# would print the whole page as one `-` and one `+` line, and the snippet's
# line cap would bound nothing. A sentence end is `.`/`!`/`?` before
# whitespace; a run with none is wrapped at word boundaries so no diff line
# outgrows MAX_SEGMENT_CHARS.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MAX_SEGMENT_CHARS = 400


@dataclass(frozen=True)
class ChangeDiff:
    """One event's diff, or why there is none.

    ``unified`` is ``difflib``'s unified diff over the two canonical texts;
    ``unavailable`` is set exactly when it could not be produced.
    """

    unified: str = ""
    unavailable: str | None = None


def diff_requested(options: ContentOptions) -> bool:
    """Whether a recipient with these options would show a diff.

    A custom body ignores the toggles (``build_body``), so it asks for one only
    by naming a diff variable.
    """
    if options.body_template:
        return any(name in options.body_template for name in DIFF_TEMPLATE_VARIABLES)
    return options.include_diff_snippet or options.include_diff_full


def _segments(text: str) -> list[str]:
    """The text as diff lines: chunk lines, split into sentences, long ones wrapped.

    Chunk boundaries (the canonical ``\\n``) always end a segment. Wrapping
    shifts after an edit, so a change inside a long unpunctuated run shows that
    whole run — bounded by the run, never the page.
    """
    out: list[str] = []
    for line in text.splitlines():
        for sentence in _SENTENCE_END.split(line):
            if len(sentence) <= MAX_SEGMENT_CHARS:
                out.append(sentence)
            else:
                out.extend(
                    textwrap.wrap(sentence, MAX_SEGMENT_CHARS, break_on_hyphens=False) or [sentence]
                )
    return out


def compute_unified_diff(previous: bytes, current: bytes) -> str:
    """Unified diff of two canonical texts, by sentence. Pure CPU — callers off
    the loop use a thread.

    ``lineterm=""`` and a ``\\n`` join: every line carries its own prefix, so
    no empty line ever appears between content lines. Hunk ranges count
    segments (``_segments``), not lines of the stored text.
    """
    before = _segments(previous.decode("utf-8", errors="replace"))
    after = _segments(current.decode("utf-8", errors="replace"))
    return "\n".join(
        difflib.unified_diff(before, after, fromfile="previous", tofile="current", lineterm="")
    )
