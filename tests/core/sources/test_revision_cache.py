"""Helpers around last_known_revisions."""

from datetime import UTC, datetime

import pytest

from src.core.sources.revision_cache import (
    get_last_fingerprint,
    upsert_last_known,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_get_returns_none_when_no_prior(db_session):
    assert await get_last_fingerprint(db_session, "01HZZ...UNSEEN00000000000") is None


@pytest.mark.asyncio
async def test_upsert_then_get_returns_fingerprint(db_session):
    await upsert_last_known(
        db_session,
        info_source_id="01HZZ00000000000000000000F",
        content_fingerprint="sha256:" + "a" * 64,
        source_revision_id="01HZZ000000000000000000REV",
        captured_at=datetime.now(UTC),
    )
    fp = await get_last_fingerprint(db_session, "01HZZ00000000000000000000F")
    assert fp == "sha256:" + "a" * 64


@pytest.mark.asyncio
async def test_upsert_overwrites_prior(db_session):
    kw = dict(
        info_source_id="01HZZ00000000000000000000F",
        captured_at=datetime.now(UTC),
    )
    await upsert_last_known(
        db_session,
        content_fingerprint="sha256:" + "a" * 64,
        source_revision_id="01HZZ000000000000000000REV",
        **kw,
    )
    await upsert_last_known(
        db_session,
        content_fingerprint="sha256:" + "b" * 64,
        source_revision_id="01HZZ00000000000000NEWREVX",
        **kw,
    )
    fp = await get_last_fingerprint(db_session, "01HZZ00000000000000000000F")
    assert fp == "sha256:" + "b" * 64
