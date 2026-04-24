"""Textual diff computation (stdlib difflib, unified format)."""

import difflib

from src.core.diff.models import DiffResult
from src.core.diff.normalize import normalize_text

_DEFAULT_CONTEXT = 3


def compute_unified_diff(
    previous: str, current: str, *, context: int = _DEFAULT_CONTEXT
) -> DiffResult:
    """Compute a unified diff between two text strings.

    Normalizes both sides (CRLF→LF, trailing whitespace) before diffing so
    that whitespace-only differences collapse to has_changes=False.
    """
    prev_norm = normalize_text(previous)
    curr_norm = normalize_text(current)

    if prev_norm == curr_norm:
        return DiffResult(unified_diff="", has_changes=False, added=0, removed=0)

    prev_lines = prev_norm.splitlines(keepends=True)
    curr_lines = curr_norm.splitlines(keepends=True)

    hunks = difflib.unified_diff(
        prev_lines,
        curr_lines,
        fromfile="previous",
        tofile="current",
        n=context,
        lineterm="",
    )
    text = "\n".join(hunks)

    added = 0
    removed = 0
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1

    return DiffResult(
        unified_diff=text,
        has_changes=(added > 0 or removed > 0),
        added=added,
        removed=removed,
    )
