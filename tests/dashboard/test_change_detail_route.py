"""Integration tests for GET /changes/{change_id} and /partials/diff/{change_id}:
diff-mount rendering, identical-snapshot fallback, mode param validation, and the
Structure segment stub."""

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
        assert b'value="structure"' in body
        assert b"disabled" in body
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
