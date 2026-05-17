"""Tests for the per-WatchedItem pipeline (`process_watched_item`).

Phase 6 / Task 7 (#160). Replaces the per-Watch `_run_check_pipeline` flow with
a per-WatchedItem pipeline that fetches the InfoItem's primary URL once,
extracts per binding (primary + cross_checks + sub_aspects), and dispatches
per child Watch — gated on whether *that* binding's fingerprint changed.

Cross_check bindings post SourceRevisions but never trigger Watch
notifications — they are selector-rot infrastructure for #157.
"""

import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select, update

from src.core.extractors.base import Chunk, ExtractionResult
from src.core.models.pending_source_revision import PendingSourceRevision
from src.core.sources.revision_cache import upsert_last_known
from src.workers.pipeline import (
    _extract_with_spec,
    _extraction_config_from_spec,
    process_watched_item,
)
from tests._information_test_models import InfoItemSource, InfoSource
from tests.conftest import (
    bind_primary_source,
    bind_sub_aspect,
    make_info_item,
    make_info_source,
    make_watch,
)

# ---------------------------------------------------------------------------
# Helpers preserved from the prior file — pure-function smoke tests
# ---------------------------------------------------------------------------


class TestExtractionConfigFromSpec:
    def test_full_page_yields_empty_selectors(self):
        config = _extraction_config_from_spec({"extraction": {"algorithm": "full_page"}})
        assert config == {"selectors": []}

    def test_css_selector_yields_single_selector_list(self):
        config = _extraction_config_from_spec(
            {"extraction": {"algorithm": "css", "selector": ".target"}}
        )
        assert config == {"selectors": [".target"]}

    def test_missing_extraction_block_defaults_to_full_page(self):
        config = _extraction_config_from_spec({})
        assert config == {"selectors": []}


class TestExtractWithSpec:
    async def test_extracts_html_with_full_page_algorithm(self):
        document = {"extraction": {"algorithm": "full_page"}}
        result = await _extract_with_spec(b"<html><body><p>Hello</p></body></html>", document)
        assert len(result.chunks) >= 1
        assert any("Hello" in c.text for c in result.chunks)

    async def test_css_selector_filters_to_matching_section(self):
        document = {"extraction": {"algorithm": "css", "selector": ".target"}}
        result = await _extract_with_spec(
            b"<html><body><div class='target'>kept</div><div>dropped</div></body></html>",
            document,
        )
        joined = " ".join(c.text for c in result.chunks)
        assert "kept" in joined
        assert "dropped" not in joined


# ---------------------------------------------------------------------------
# process_watched_item scenarios (Task 7.2)
# ---------------------------------------------------------------------------


def _make_extraction(text: str) -> ExtractionResult:
    """Build an ExtractionResult containing a single chunk of *text*."""
    return ExtractionResult(chunks=[Chunk(index=0, chunk_type="paragraph", label="p", text=text)])


def _fp(text: str) -> str:
    """Return the fingerprint the pipeline will produce for an extraction of *text*."""
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


async def _seed_cache(session, *, info_source_id: str, text: str) -> str:
    """Seed the local revision cache for *info_source_id* so the fingerprint matches *text*."""
    fp = _fp(text)
    await upsert_last_known(
        session,
        info_source_id=info_source_id,
        content_fingerprint=fp,
        source_revision_id="01HZZ000000000000000000OLD",
        captured_at=datetime.now(UTC),
    )
    return fp


def _post_response(source_revision_id: str) -> MagicMock:
    """Build a fake post_source_revision response."""
    out = MagicMock()
    out.source_revision_id = source_revision_id
    return out


async def _build_watched_item_with_primary_and_sub(db_session):
    """Create an InfoItem with a primary binding + one sub_aspect binding.

    Returns ``(watched_item, primary_source_id, sub_source_id)`` (ids are strings).
    Side effect: a WatchedItem row is created and Watches can attach to it.
    """
    info_item = await make_info_item(db_session, name="Test InfoItem")
    primary_src = await make_info_source(db_session, url="https://example.com/page")
    sub_src = await make_info_source(db_session, parent_info_source_id=primary_src.info_source_id)
    await bind_primary_source(
        db_session,
        info_item_id=info_item.info_item_id,
        info_source_id=primary_src.info_source_id,
    )
    await bind_sub_aspect(
        db_session,
        info_item_id=info_item.info_item_id,
        info_source_id=sub_src.info_source_id,
    )
    # make_watch attaches/creates the parent WatchedItem; we want one Watch to
    # land before we read .watched_item so the WatchedItem exists.
    primary_watch = await make_watch(
        db_session,
        name="Primary watch",
        info_item_id=info_item.info_item_id,
    )
    return (
        primary_watch.watched_item,
        str(primary_src.info_source_id),
        str(sub_src.info_source_id),
        primary_watch,
    )


def _patch_extract(by_source_id: dict[str, str]) -> patch:
    """Patch `_extract_with_spec` so it returns text keyed off the *info_source_id*.

    Bindings carry ``source_spec`` which is a MagicMock(additional_properties=…).
    We can't key on info_source_id from the spec alone, so we patch the
    extractor to inspect a sentinel embedded in the spec doc.
    """

    async def _fake_extract(raw_content, document):
        # document is the JSONB additional_properties dict our info_client
        # fixture returns. We've stamped an id sentinel into it below.
        marker = document.get("_test_marker") if isinstance(document, dict) else None
        text = by_source_id.get(marker, "default")
        return _make_extraction(text)

    return patch("src.workers.pipeline._extract_with_spec", side_effect=_fake_extract)


async def _stamp_marker(db_session, info_source_id: str, marker: str) -> None:
    """Stamp a `_test_marker` field into the InfoSource's source_spec.

    The `info_client` fixture surfaces ``source_spec.additional_properties``
    as the InfoSource's raw spec dict, which is what `_extract_with_spec`
    consumes. Tagging the JSONB lets our patched extractor pick the right
    fake extraction result without colliding with ULIDs.
    """
    row = (
        await db_session.execute(
            select(InfoSource).where(InfoSource.info_source_id == info_source_id)
        )
    ).scalar_one()
    new_spec = {**(row.source_spec or {}), "_test_marker": marker}
    await db_session.execute(
        update(InfoSource)
        .where(InfoSource.info_source_id == info_source_id)
        .values(source_spec=new_spec)
    )
    await db_session.flush()


@pytest.mark.integration
class TestProcessWatchedItem:
    """End-to-end behaviour of the per-WatchedItem pipeline."""

    async def test_a_primary_changed_subaspect_unchanged(
        self, db_session, info_client, tmp_path, monkeypatch
    ):
        """Primary fingerprint changes; sub_aspect cache-hits.

        Expected:
          * POST SourceRevision for primary (1 call).
          * No POST for sub_aspect (fast-path).
          * Dispatch WatchEvent for the primary Watch.
          * Do NOT dispatch for the sub_aspect Watch.
        """
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        (
            watched_item,
            primary_src_id,
            sub_src_id,
            w_primary,
        ) = await _build_watched_item_with_primary_and_sub(db_session)
        await _stamp_marker(db_session, primary_src_id, "PRIMARY")
        await _stamp_marker(db_session, sub_src_id, "SUB")

        # Sub-aspect Watch attached to the SAME WatchedItem.
        w_sub = await make_watch(
            db_session,
            name="Sub-aspect watch",
            info_item_id=watched_item.info_item_id,
            target_info_source_id=sub_src_id,
            watched_item=watched_item,
        )

        # Seed cache so sub_aspect is unchanged but primary is not.
        await _seed_cache(db_session, info_source_id=sub_src_id, text="old_sub")

        # Wire the Archiver client mock — only post_source_revision needs adding;
        # get_info_item + get_info_source already come from the fixture.
        info_client.post_source_revision = AsyncMock(
            return_value=_post_response("01HZZ000000000000000000NEW")
        )

        extracts = {"PRIMARY": "new_primary", "SUB": "old_sub"}
        dispatched: list = []

        async def _capture_dispatch(session, event):
            dispatched.append(event)

        with (
            _patch_extract(extracts),
            patch(
                "src.workers.pipeline.dispatch_event_notifications",
                side_effect=_capture_dispatch,
            ),
        ):
            result = await process_watched_item(
                session=db_session,
                info_client=info_client,
                watched_item=watched_item,
                raw_content=b"<html><body>doesn't-matter</body></html>",
            )

        # One POST: primary only (sub was cache-hit).
        assert info_client.post_source_revision.await_count == 1
        called_with = info_client.post_source_revision.await_args_list[0].kwargs
        assert str(called_with["info_source_id"]) == primary_src_id

        # One dispatch: the primary Watch.
        assert len(dispatched) == 1
        assert dispatched[0].watch_id == str(w_primary.id)
        assert dispatched[0].watch_id != str(w_sub.id)

        assert result.revisions_posted == 1
        assert result.cache_hits >= 1  # sub was cache-hit
        assert result.notifications_dispatched == 1

    async def test_b_subaspect_changed_primary_unchanged(
        self, db_session, info_client, tmp_path, monkeypatch
    ):
        """Sub_aspect changes; primary cache-hits.

        Expected: skip primary POST, post sub_aspect, dispatch only the sub Watch.
        """
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        (
            watched_item,
            primary_src_id,
            sub_src_id,
            w_primary,
        ) = await _build_watched_item_with_primary_and_sub(db_session)
        await _stamp_marker(db_session, primary_src_id, "PRIMARY")
        await _stamp_marker(db_session, sub_src_id, "SUB")

        w_sub = await make_watch(
            db_session,
            name="Sub-aspect watch",
            info_item_id=watched_item.info_item_id,
            target_info_source_id=sub_src_id,
            watched_item=watched_item,
        )

        # Seed primary cache to match.
        await _seed_cache(db_session, info_source_id=primary_src_id, text="old_primary")

        info_client.post_source_revision = AsyncMock(
            return_value=_post_response("01HZZ000000000000000000SUB")
        )

        extracts = {"PRIMARY": "old_primary", "SUB": "new_sub"}
        dispatched: list = []

        async def _capture(session, event):
            dispatched.append(event)

        with (
            _patch_extract(extracts),
            patch(
                "src.workers.pipeline.dispatch_event_notifications",
                side_effect=_capture,
            ),
        ):
            await process_watched_item(
                session=db_session,
                info_client=info_client,
                watched_item=watched_item,
                raw_content=b"<html><body>nope</body></html>",
            )

        # One POST: sub_aspect only.
        assert info_client.post_source_revision.await_count == 1
        kwargs = info_client.post_source_revision.await_args_list[0].kwargs
        assert str(kwargs["info_source_id"]) == sub_src_id

        # One dispatch: the sub Watch.
        assert len(dispatched) == 1
        assert dispatched[0].watch_id == str(w_sub.id)
        # Primary Watch must NOT be dispatched.
        assert all(e.watch_id != str(w_primary.id) for e in dispatched)

    async def test_c_both_changed(self, db_session, info_client, tmp_path, monkeypatch):
        """Primary AND sub_aspect both change → both posted, both Watches dispatched."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        (
            watched_item,
            primary_src_id,
            sub_src_id,
            w_primary,
        ) = await _build_watched_item_with_primary_and_sub(db_session)
        await _stamp_marker(db_session, primary_src_id, "PRIMARY")
        await _stamp_marker(db_session, sub_src_id, "SUB")

        w_sub = await make_watch(
            db_session,
            name="Sub-aspect watch",
            info_item_id=watched_item.info_item_id,
            target_info_source_id=sub_src_id,
            watched_item=watched_item,
        )

        # No cache seeded; both extractions are fresh → both fingerprints diff.
        info_client.post_source_revision = AsyncMock(
            side_effect=[
                _post_response("01HZZ00000000000000000NEW1"),
                _post_response("01HZZ00000000000000000NEW2"),
            ]
        )

        extracts = {"PRIMARY": "new_primary", "SUB": "new_sub"}
        dispatched: list = []

        async def _capture(session, event):
            dispatched.append(event)

        with (
            _patch_extract(extracts),
            patch(
                "src.workers.pipeline.dispatch_event_notifications",
                side_effect=_capture,
            ),
        ):
            result = await process_watched_item(
                session=db_session,
                info_client=info_client,
                watched_item=watched_item,
                raw_content=b"<html><body>both</body></html>",
            )

        # Both bindings posted.
        assert info_client.post_source_revision.await_count == 2
        posted_ids = {
            str(call.kwargs["info_source_id"])
            for call in info_client.post_source_revision.await_args_list
        }
        assert posted_ids == {primary_src_id, sub_src_id}

        # Both Watches dispatched.
        dispatched_watch_ids = {e.watch_id for e in dispatched}
        assert str(w_primary.id) in dispatched_watch_ids
        assert str(w_sub.id) in dispatched_watch_ids
        assert result.notifications_dispatched == 2

    async def test_d_cross_check_posts_but_never_dispatches(
        self, db_session, info_client, tmp_path, monkeypatch
    ):
        """Cross_check binding posts SourceRevision but no Watch ever fires for it.

        Setup: WatchedItem with primary + cross_check (NOT sub_aspect).
        Primary cache-hits; cross_check's selector now extracts different text →
        cache miss → SourceRevision posted for cross_check, no Watch notification.
        """
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        info_item = await make_info_item(db_session, name="Cross-check item")
        primary_src = await make_info_source(db_session, url="https://example.com/x")
        cross_src = await make_info_source(
            db_session, parent_info_source_id=primary_src.info_source_id
        )
        await bind_primary_source(
            db_session,
            info_item_id=info_item.info_item_id,
            info_source_id=primary_src.info_source_id,
        )
        # Cross-check binding (not via the fixture helper — we want role='cross_check').
        db_session.add(
            InfoItemSource(
                info_item_id=info_item.info_item_id,
                info_source_id=cross_src.info_source_id,
                role="cross_check",
            )
        )
        await db_session.flush()

        await _stamp_marker(db_session, str(primary_src.info_source_id), "PRIMARY")
        await _stamp_marker(db_session, str(cross_src.info_source_id), "CROSS")

        w_primary = await make_watch(
            db_session,
            name="Primary watch",
            info_item_id=info_item.info_item_id,
        )
        watched_item = w_primary.watched_item

        # Seed primary cache so primary is unchanged.
        await _seed_cache(
            db_session, info_source_id=str(primary_src.info_source_id), text="same_primary"
        )

        info_client.post_source_revision = AsyncMock(
            return_value=_post_response("01HZZ00000000000000CROSS01")
        )

        extracts = {"PRIMARY": "same_primary", "CROSS": "drifted_cross_value"}
        dispatched: list = []

        async def _capture(session, event):
            dispatched.append(event)

        with (
            _patch_extract(extracts),
            patch(
                "src.workers.pipeline.dispatch_event_notifications",
                side_effect=_capture,
            ),
        ):
            await process_watched_item(
                session=db_session,
                info_client=info_client,
                watched_item=watched_item,
                raw_content=b"<html><body>cross-check</body></html>",
            )

        # Exactly one POST — the cross_check, NOT the primary (cache-hit).
        assert info_client.post_source_revision.await_count == 1
        kwargs = info_client.post_source_revision.await_args_list[0].kwargs
        assert str(kwargs["info_source_id"]) == str(cross_src.info_source_id)

        # No dispatches — cross_check never fires a WatchEvent regardless of
        # whether its fingerprint changed.
        assert dispatched == []

    async def test_outbox_path_on_post_failure_preserves_cycle(
        self, db_session, info_client, tmp_path, monkeypatch
    ):
        """POST failure for a binding enqueues to outbox; cycle continues."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        (
            watched_item,
            primary_src_id,
            sub_src_id,
            w_primary,
        ) = await _build_watched_item_with_primary_and_sub(db_session)
        await _stamp_marker(db_session, primary_src_id, "PRIMARY")
        await _stamp_marker(db_session, sub_src_id, "SUB")

        info_client.post_source_revision = AsyncMock(side_effect=httpx.ConnectError("refused"))

        extracts = {"PRIMARY": "new_primary", "SUB": "old_sub"}
        await _seed_cache(db_session, info_source_id=sub_src_id, text="old_sub")

        with (
            _patch_extract(extracts),
            patch(
                "src.workers.pipeline.dispatch_event_notifications", new=AsyncMock()
            ) as mock_dispatch,
        ):
            await process_watched_item(
                session=db_session,
                info_client=info_client,
                watched_item=watched_item,
                raw_content=b"<html><body>x</body></html>",
            )

        # Primary POST attempted, raised → outbox row landed.
        rows = (
            (
                await db_session.execute(
                    select(PendingSourceRevision).where(
                        PendingSourceRevision.info_source_id == primary_src_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

        # No dispatch — POST failed so notification fires only on Archiver-acked
        # writes (outbox drain handles late notification).
        assert mock_dispatch.await_count == 0
        # Reference w_primary to keep linter quiet
        assert w_primary is not None

    async def test_removed_subaspect_binding_logs_and_skips(
        self, db_session, info_client, tmp_path, monkeypatch
    ):
        """A sub_aspect Watch pointing at a binding no longer present logs + skips.

        Models the race where Archiver deactivates a sub_aspect binding between
        check cycles. The Watch row still references it (FK is RESTRICT but
        the binding row is what's gone); we log a warning and continue.
        """
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        # Build an InfoItem with primary only; no sub_aspect binding.
        info_item = await make_info_item(db_session, name="Orphan target")
        primary_src = await make_info_source(db_session, url="https://example.com/orphan")
        await bind_primary_source(
            db_session,
            info_item_id=info_item.info_item_id,
            info_source_id=primary_src.info_source_id,
        )
        await _stamp_marker(db_session, str(primary_src.info_source_id), "PRIMARY")

        # Make a "ghost" InfoSource — exists in the DB but has NO binding to
        # the InfoItem. The sub Watch points at it.
        ghost_src = await make_info_source(
            db_session, parent_info_source_id=primary_src.info_source_id
        )

        w_primary = await make_watch(
            db_session,
            name="Primary",
            info_item_id=info_item.info_item_id,
        )
        watched_item = w_primary.watched_item

        await make_watch(
            db_session,
            name="Ghost sub-aspect",
            info_item_id=info_item.info_item_id,
            target_info_source_id=ghost_src.info_source_id,
            watched_item=watched_item,
        )

        # Seed primary so it's unchanged → no dispatch for primary either.
        await _seed_cache(
            db_session,
            info_source_id=str(primary_src.info_source_id),
            text="same",
        )

        info_client.post_source_revision = AsyncMock(return_value=_post_response("X"))

        dispatched: list = []

        async def _capture(session, event):
            dispatched.append(event)

        with (
            _patch_extract({"PRIMARY": "same"}),
            patch(
                "src.workers.pipeline.dispatch_event_notifications",
                side_effect=_capture,
            ),
        ):
            await process_watched_item(
                session=db_session,
                info_client=info_client,
                watched_item=watched_item,
                raw_content=b"<html><body>z</body></html>",
            )

        # No dispatch: primary unchanged, ghost sub_aspect skipped with log.
        assert dispatched == []
