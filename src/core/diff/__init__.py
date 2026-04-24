"""Diff service: normalize → compute → render.

Shared by dashboard change-detail views and (future) notification output.
See docs/plans/2026-04-24-diff-viewer-phase-a.md and issues #115/#116/#117.
"""

from src.core.diff.models import DiffResult
from src.core.diff.textual import compute_unified_diff

__all__ = ["DiffResult", "compute_unified_diff"]
