#!/usr/bin/env python3
"""One-shot script: migrate watcher Apprise channels to the notifier service.

For every WatchNotificationConfig and NotificationTemplate row that has no
remote_channel_id set, this script:

  1. Decrypts the stored Apprise URL with watcher's APPRISE_SECRET_KEY.
  2. POSTs it to notifier's POST /api/v1/channels endpoint.
  3. Writes the returned channel ULID back to the local row.

Run this script ONCE before setting USE_REMOTE_NOTIFY=1 in
/etc/watcher/.env. After all rows are migrated, consider rotating
APPRISE_SECRET_KEY so that leaked DB dumps cannot decrypt the old URLs.

Usage:
    export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
    uv run python scripts/migrate_channels_to_notifier.py [--dry-run]

Options:
    --dry-run   Print what would be done without writing to either DB.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.core.crypto import decrypt_apprise_url
from src.core.logging import configure_logging, get_logger
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import NotificationTemplate
from src.core.notifier_client import get_notifier_client

logger = get_logger(__name__)


async def _migrate_rows(
    session: AsyncSession,
    rows: list,
    channel_name_fn: Callable,
    label_fn: Callable,
    dry_run: bool,
) -> tuple[int, int]:
    """Migrate a list of model rows to notifier channels. Returns (migrated, skipped)."""
    async with get_notifier_client() as client:
        migrated = skipped = 0

        for row in rows:
            label = label_fn(row)
            try:
                plaintext_url = decrypt_apprise_url(row.apprise_url)
            except Exception as exc:
                logger.warning(f"{label}: decrypt failed, skipping — {exc}")
                skipped += 1
                continue

            channel_name = channel_name_fn(row)
            if dry_run:
                logger.info(f"[dry-run] would create channel '{channel_name}' for {label}")
                migrated += 1
                continue

            try:
                channel = await client.channels.create(
                    name=channel_name,
                    apprise_url=plaintext_url,
                    channel_hint=row.channel_hint or None,
                )
                row.remote_channel_id = channel.id
                migrated += 1
                logger.info(f"{label}: created remote channel {channel.id}")
            except Exception as exc:
                logger.warning(f"{label}: notifier channel creation failed, skipping — {exc}")
                skipped += 1

    if not dry_run:
        await session.commit()

    return migrated, skipped


async def _migrate_local_configs(session: AsyncSession, dry_run: bool) -> tuple[int, int]:
    """Migrate WatchNotificationConfig rows. Returns (migrated, skipped)."""
    result = await session.execute(
        select(WatchNotificationConfig).where(WatchNotificationConfig.remote_channel_id.is_(None))
    )
    rows = list(result.scalars().all())
    return await _migrate_rows(
        session,
        rows,
        channel_name_fn=lambda r: f"watcher-local-{r.id}",
        label_fn=lambda r: f"WatchNotificationConfig id={r.id}",
        dry_run=dry_run,
    )


async def _migrate_templates(session: AsyncSession, dry_run: bool) -> tuple[int, int]:
    """Migrate NotificationTemplate rows. Returns (migrated, skipped)."""
    result = await session.execute(
        select(NotificationTemplate).where(NotificationTemplate.remote_channel_id.is_(None))
    )
    rows = list(result.scalars().all())
    return await _migrate_rows(
        session,
        rows,
        channel_name_fn=lambda r: f"watcher-template-{r.id}",
        label_fn=lambda r: f"NotificationTemplate id={r.id} title={r.title!r}",
        dry_run=dry_run,
    )


async def main(dry_run: bool) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set")
        sys.exit(1)

    engine = create_async_engine(database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        lm, ls = await _migrate_local_configs(session, dry_run)
        tm, ts = await _migrate_templates(session, dry_run)

    await engine.dispose()

    total_migrated = lm + tm
    total_skipped = ls + ts
    mode = "[dry-run] " if dry_run else ""
    logger.info(
        f"{mode}migration complete: {total_migrated} channels created, {total_skipped} skipped"
    )
    if total_skipped:
        sys.exit(1)


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print plan without writing anything"
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
