"""DTOs for the diff service."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiffResult:
    """Outcome of a textual diff.

    unified_diff — standard unified-diff text (empty if no changes).
    has_changes  — true iff at least one line was added or removed.
    added        — count of added lines.
    removed      — count of removed lines.
    """

    unified_diff: str
    has_changes: bool
    added: int
    removed: int
