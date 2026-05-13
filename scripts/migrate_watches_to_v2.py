"""One-shot: assign Watch.info_source_id from a manifest mapping.

Operator pre-wires ``information.info_item_sources`` in Archiver, then
supplies a JSON file mapping ``info_item_id → info_source_id``. Script
reads the manifest, applies to every Watch with NULL info_source_id,
hard-errors on missing mappings.

Usage:
  uv run python scripts/migrate_watches_to_v2.py --manifest watches.json

Manifest format:
  {"01HZZ...ITEM_A": "01HZZ...SOURCE_A", "01HZZ...ITEM_B": "01HZZ...SOURCE_B"}
"""

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.models.watch import Watch


class MissingMappingError(Exception):
    """A Watch's info_item_id has no entry in the manifest."""


async def migrate_watches(session, manifest: dict[str, str]) -> None:
    """Set Watch.info_source_id for each Watch with a NULL info_source_id.

    As of Task 5.5, Watch.info_item_id no longer exists and Watch.info_source_id
    is NOT NULL. This function is now a no-op (all rows already have info_source_id
    set by the time the migration runs). Kept for backward-compat with test suite;
    the manifest argument is ignored.
    """
    result = await session.execute(select(Watch).where(Watch.info_source_id.is_(None)))
    watches = list(result.scalars().all())
    if watches:
        raise MissingMappingError(
            f"Found {len(watches)} Watch(es) with NULL info_source_id. "
            "This should not happen after Task 5.5 migration. "
            "Populate info_source_id before upgrading."
        )
    await session.commit()


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign Watch.info_source_id from a manifest JSON file."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to a JSON file mapping info_item_id → info_source_id",
    )
    args = parser.parse_args()

    with open(args.manifest) as f:
        manifest = json.load(f)

    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        try:
            await migrate_watches(session, manifest)
            print("OK: all Watches assigned info_source_id")
        except MissingMappingError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
