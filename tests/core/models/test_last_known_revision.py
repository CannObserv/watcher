"""Round-trip tests for last_known_revisions."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.models.last_known_revision import LastKnownRevision

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_last_known_revision_round_trip(db_session):
    row = LastKnownRevision(
        info_source_id="01HZZ00000000000000000000F",
        content_fingerprint="sha256:" + "a" * 64,
        source_revision_id="01HZZ000000000000000000REV",
        captured_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(LastKnownRevision).where(LastKnownRevision.info_source_id == row.info_source_id)
        )
    ).scalar_one()
    assert fetched.content_fingerprint == row.content_fingerprint
