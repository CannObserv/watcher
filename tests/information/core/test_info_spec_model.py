"""InfoSpec ORM tests — round-trip + partial unique constraint."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.information.core.models import InfoItem, InfoSpec


def _doc() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com"},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


@pytest.mark.asyncio
async def test_info_spec_round_trip(session):
    item = InfoItem(name="A")
    session.add(item)
    await session.flush()

    spec = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1,
        document=_doc(),
        priority=1,
        active=True,
    )
    session.add(spec)
    await session.commit()

    result = await session.execute(
        select(InfoSpec).where(InfoSpec.info_spec_id == spec.info_spec_id)
    )
    fetched = result.scalar_one()
    assert fetched.priority == 1
    assert fetched.active is True
    assert fetched.document["target"]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_partial_unique_active_priority_blocks_duplicate(session):
    item = InfoItem(name="A")
    session.add(item)
    await session.flush()

    spec1 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1,
        document=_doc(),
        priority=1,
        active=True,
    )
    session.add(spec1)
    await session.commit()

    spec2 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1,
        document=_doc(),
        priority=1,
        active=True,
    )
    session.add(spec2)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_inactive_specs_can_share_priority(session):
    """Two inactive specs at priority=1 should NOT violate the partial unique."""
    item = InfoItem(name="A")
    session.add(item)
    await session.flush()

    spec1 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1,
        document=_doc(),
        priority=1,
        active=False,
    )
    spec2 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1,
        document=_doc(),
        priority=1,
        active=False,
    )
    session.add_all([spec1, spec2])
    await session.commit()  # should succeed
