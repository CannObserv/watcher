"""Tests for structural HTML diff (Phase B.2 stub — Phase A scaffold)."""

import pytest

from src.core.diff.structural import compute_html_tree_diff


class TestComputeHtmlTreeDiffStub:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="Phase B"):
            compute_html_tree_diff("<p>a</p>", "<p>b</p>")
