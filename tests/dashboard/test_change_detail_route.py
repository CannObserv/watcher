"""Integration tests for GET /changes/{change_id} and /partials/diff/{change_id}:
diff-mount rendering, identical-snapshot fallback, mode param validation, the
Structure segment stub, and Raw-mode HTML pretty-print (#118)."""

import re

import pytest

pytestmark = pytest.mark.integration


class TestChangeDetailRoute:
    async def test_response_contains_diff_mount_when_snapshots_differ(
        self, client, make_change_with_snapshots
    ):
        change = await make_change_with_snapshots(
            prev_text="hello\nworld\n",
            curr_text="hello\nplanet\n",
            write_files=True,
            change_metadata={"added": [], "modified": ["Main"], "removed": []},
        )
        resp = await client.get(f"/changes/{change.id}")
        assert resp.status_code == 200
        assert b"data-unified-diff" in resp.content
        assert b"diff-mount" in resp.content

    async def test_response_shows_no_changes_when_snapshots_identical(
        self, client, make_change_with_snapshots
    ):
        change = await make_change_with_snapshots(
            prev_text="same\ncontent\n",
            curr_text="same\ncontent\n",
            write_files=True,
            change_metadata={"added": [], "modified": [], "removed": []},
        )
        resp = await client.get(f"/changes/{change.id}")
        assert resp.status_code == 200
        assert b"No textual differences found" in resp.content
        assert b"data-unified-diff" not in resp.content

    async def test_unknown_mode_returns_422(self, client, make_change_with_snapshots):
        """Literal['extracted','raw'] rejects unknown modes with FastAPI's 422."""
        change = await make_change_with_snapshots(
            prev_text="a\n", curr_text="b\n", write_files=True
        )
        resp = await client.get(f"/changes/{change.id}?mode=bogus")
        assert resp.status_code == 422

    async def test_structure_segment_is_disabled(self, client, make_change_with_snapshots):
        """The Structure tab stub ships disabled with an sr-only hint until Phase B."""
        change = await make_change_with_snapshots(
            prev_text="a\n", curr_text="b\n", write_files=True
        )
        resp = await client.get(f"/changes/{change.id}")
        assert resp.status_code == 200
        body = resp.content
        # The `disabled` attribute must be on the Structure radio input itself,
        # not just on the surrounding `segment-disabled` class / `aria-disabled`.
        assert re.search(rb'<input[^>]+value="structure"[^>]*\bdisabled\b', body) is not None
        assert b'id="structure-coming-soon-hint"' in body
        assert b"Structural diff coming soon" in body


class TestPartialDiffRoute:
    @pytest.mark.parametrize("mode", ["extracted", "raw"])
    async def test_valid_mode_returns_200_with_mount(
        self, client, make_change_with_snapshots, mode
    ):
        """Both valid modes exercise the storage_path vs text_path branch in the route."""
        change = await make_change_with_snapshots(
            prev_text="hello\nworld\n",
            curr_text="hello\nplanet\n",
            write_files=True,
        )
        resp = await client.get(f"/partials/diff/{change.id}?mode={mode}")
        assert resp.status_code == 200
        assert b"data-unified-diff" in resp.content

    async def test_unknown_mode_returns_422(self, client, make_change_with_snapshots):
        change = await make_change_with_snapshots(
            prev_text="a\n", curr_text="b\n", write_files=True
        )
        resp = await client.get(f"/partials/diff/{change.id}?mode=bogus")
        assert resp.status_code == 422

    async def test_raw_mode_prettifies_html_for_html_watches(
        self, client, make_change_with_snapshots
    ):
        """Issue #118: Raw mode on an HTML watch must pretty-print so a single-line
        page renders as multi-line in the diff (each block element on its own line)."""
        # Single-line HTML where the change is buried near the end — the bug case.
        prev = (
            "<html><body><div><p>Header</p>"
            "<p>Body intro</p><p>Final word: red</p></div></body></html>"
        )
        curr = (
            "<html><body><div><p>Header</p>"
            "<p>Body intro</p><p>Final word: blue</p></div></body></html>"
        )
        change = await make_change_with_snapshots(prev_text=prev, curr_text=curr, write_files=True)
        resp = await client.get(f"/partials/diff/{change.id}?mode=raw")
        assert resp.status_code == 200
        body = resp.content.decode()
        # The unified diff is in data-unified-diff. With normalization, the
        # change line is short (just the changed <p>), not the whole document.
        # Verify by checking that the changed <p> appears as a self-contained
        # `+` line, not embedded in a multi-thousand-char single line.
        match = re.search(r"\+\s*&lt;p&gt;Final word: blue&lt;/p&gt;", body)
        assert match is not None, (
            "Raw HTML diff didn't isolate the changed line — pretty-print not applied?"
        )

    async def test_raw_mode_does_not_prettify_non_html(self, client, make_change_with_snapshots):
        """Plain-text watches must not get HTML pretty-print (would wrap in <html><body>)."""
        change = await make_change_with_snapshots(
            prev_text="line one\nline two\n",
            curr_text="line one\nline THREE\n",
            write_files=True,
            watch_content_type="file",
        )
        resp = await client.get(f"/partials/diff/{change.id}?mode=raw")
        assert resp.status_code == 200
        body = resp.content
        # The text should appear as-is, not wrapped in <html><body>...
        assert b"<html>" not in body
        assert b"line two" in body
        assert b"line THREE" in body
