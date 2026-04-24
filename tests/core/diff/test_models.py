"""Tests for diff result DTOs."""

from src.core.diff.models import DiffResult


class TestDiffResult:
    def test_construct_empty(self):
        r = DiffResult(unified_diff="", has_changes=False, added=0, removed=0)
        assert r.unified_diff == ""
        assert r.has_changes is False
        assert r.added == 0
        assert r.removed == 0

    def test_construct_with_changes(self):
        r = DiffResult(
            unified_diff="--- previous\n+++ current\n@@ -1 +1 @@\n-a\n+b\n",
            has_changes=True,
            added=1,
            removed=1,
        )
        assert r.has_changes is True
        assert "@@" in r.unified_diff

    def test_is_frozen(self):
        """DiffResult is immutable — safe to share across templates."""
        import dataclasses

        r = DiffResult(unified_diff="", has_changes=False, added=0, removed=0)
        try:
            r.added = 5  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            return
        raise AssertionError("DiffResult should be frozen")
