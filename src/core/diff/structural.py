"""Structural (tree) diff for HTML.

Phase A: stub. Phase B.2 replaces with xmldiff-based tree edit-distance
diff. (#118 was Phase B.1 — pretty-print normalisation in normalize.py.)
See issue #115 for the rollout plan.
"""


def compute_html_tree_diff(previous: str, current: str) -> list:
    """Compute a structural diff between two HTML documents.

    Phase A stub — always raises. Phase B.2 implements via `xmldiff`.
    """
    raise NotImplementedError(
        "compute_html_tree_diff is reserved for issue #115 Phase B.2 (xmldiff)"
    )
