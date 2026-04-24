"""Tests for structural HTML diff (Phase B stub in Phase A)."""

import pytest

from src.core.diff.structural import compute_html_tree_diff


class TestComputeHtmlTreeDiffStub:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="Phase B"):
            compute_html_tree_diff("<p>a</p>", "<p>b</p>")
