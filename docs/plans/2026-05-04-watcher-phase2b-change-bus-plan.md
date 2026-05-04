# Phase 2b — Watcher Change Bus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Stand up the Redis-backed Change bus infrastructure that Watcher will use (in Phase 2c) to publish detected source-data Changes to downstream consumers (Archive, etc.). Ships the `ChangePublisher` class, the local outbox on the existing `changes` table, a drain worker, and a reference consumer that proves the wire end-to-end.

**Architecture:** `redis-py` async client wrapped in a concrete `ChangePublisher` class (no abstraction layer per design-doc YAGNI). Local outbox columns added to the existing `changes` table; Procrastinate periodic task drains unpublished rows by calling `publish_change`. Reference consumer at `tools/info_changes_consumer.py` runs an `XREADGROUP` loop and writes received events to JSONL.

**Tech Stack:** Redis 7 (system service), `redis>=5.0,<6` (Python async client), `fakeredis>=2.20,<3` (dev/test), Procrastinate (already in repo).

**Reference:** Design doc at `docs/plans/2026-05-03-information-source-specifications-design.md`, Phase 2 section. Specifically: "Implement concrete `ChangePublisher` (Redis Streams, no Protocol). Add Redis to deployment. Local outbox via the existing `changes` table; drain worker via Procrastinate. Reference consumer in `tools/`."

**Scope deferral (NOT in 2b):** Actually wiring the drain worker's payload shape to the real `info.changes` event schema (`info_item_id`, `info_spec_id`, fingerprints, fetched_at) — that requires the Information SDK + Watch refactor and lands in 2c. 2b ships a generic envelope built from the existing `changes` row fields; 2c refines.

---

## Pre-flight

This plan executes from the worktree at `/home/exedev/watcher/.worktrees/feat-138-watcher-phase2b-change-bus` on branch `feat/138-watcher-phase2b-change-bus`. Every Bash command must `cd` into the worktree first or chain with `&&`.

### Redis prereq — needs user authorization

Redis is not yet installed on the VM. Before starting Task 1, the user must run:

```bash
sudo apt install -y redis-server
sudo systemctl enable --now redis-server
redis-cli ping   # expect PONG
```

The plan assumes Redis is running on `localhost:6379` (default). The `REDIS_URL` env var defaults to `redis://localhost:6379/0` if unset.

Tests use `fakeredis` and don't need a real Redis; the smoke test in Task 8 does.

### File structure

**Created:**
```
src/core/changes/
  __init__.py
  publisher.py          # ChangePublisher (Redis Streams concrete impl)
  outbox.py             # Helpers for marking rows published/unpublished

alembic/versions/
  <hash>_add_changes_outbox_columns.py    # adds published_to_bus_at, bus_message_id

src/workers/
  changes_drain.py      # Procrastinate periodic task (drain unpublished changes)

tools/
  info_changes_consumer.py    # XREADGROUP reference consumer

deploy/
  redis-server.dropin.conf    # (optional) systemd drop-in if we need to tune Redis

tests/core/changes/
  test_publisher.py
  test_outbox.py

tests/workers/
  test_changes_drain.py
```

**Modified:**
- `pyproject.toml` — add `redis>=5.0,<6` to deps, `fakeredis>=2.20,<3` to dev deps
- `src/core/models/change.py` — add `published_to_bus_at` and `bus_message_id` columns
- `tests/conftest.py` — possibly extend the `test_engine` fixture if migrations introduce triggers (none planned for 2b)
- `AGENTS.md` — env var docs (`REDIS_URL`)
- `docs/COMMANDS.md` — reference consumer invocation

**NOT modified in 2b:** `src/api/routes/`, `src/core/watches.py`, `src/workers/tasks.py` change-detection paths. The drain worker reads the outbox; integration with detection paths is Phase 2c.

---

## Task 1: Add Redis + fakeredis dependencies + REDIS_URL env helper

**Files:**
- Modify: `pyproject.toml`
- Create: `src/core/changes/__init__.py` (empty for now)
- Create: `src/core/changes/redis_url.py`
- Create: `tests/core/changes/__init__.py` (empty)
- Create: `tests/core/changes/test_redis_url.py`

- [ ] **Step 1: Add deps to `pyproject.toml`**

In the `dependencies = [ ... ]` block, add `"redis>=5.0,<6"` (alphabetical insertion).

In the `[dependency-groups] dev = [ ... ]` block, add `"fakeredis>=2.20,<3"`.

Run `uv sync` from the worktree.

- [ ] **Step 2: Create the redis-url helper**

`src/core/changes/redis_url.py`:
```python
"""Read REDIS_URL with a sensible default for prototype/dev."""

import os


def get_redis_url() -> str:
    """Return the Redis URL.

    Reads REDIS_URL env var. Defaults to redis://localhost:6379/0 for
    prototype convenience (Redis runs on the same VM as Watcher in Phase 2).
    """
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")
```

- [ ] **Step 3: Test the helper**

`tests/core/changes/test_redis_url.py`:
```python
"""REDIS_URL resolution tests."""

from src.core.changes.redis_url import get_redis_url


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert get_redis_url() == "redis://localhost:6379/0"


def test_uses_env_var_when_set(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid:6379/3")
    assert get_redis_url() == "redis://example.invalid:6379/3"
```

Also create `src/core/changes/__init__.py` and `tests/core/changes/__init__.py` as empty files.

- [ ] **Step 4: Run tests**

```bash
cd /home/exedev/watcher/.worktrees/feat-138-watcher-phase2b-change-bus && \
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs) && \
  uv run pytest tests/core/changes/ --no-cov -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/exedev/watcher/.worktrees/feat-138-watcher-phase2b-change-bus && \
  git add pyproject.toml uv.lock src/core/changes tests/core/changes && \
  git commit -m "#138 feat: add redis + fakeredis deps + REDIS_URL helper"
```

---

## Task 2: DB migration — add outbox columns to `changes`

**Files:**
- Modify: `src/core/models/change.py`
- Create: `alembic/versions/<hash>_add_changes_outbox_columns.py`

**Schema additions to `changes` table:**
- `published_to_bus_at: TIMESTAMPTZ NULL` — set when the row has been successfully published.
- `bus_message_id: VARCHAR(64) NULL` — the Redis Stream message ID returned by XADD.

- [ ] **Step 1: Update the model**

In `src/core/models/change.py`, add after `detected_at`:
```python
    published_to_bus_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bus_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

Add `String` to the `from sqlalchemy import ...` line.

- [ ] **Step 2: Generate migration**

```bash
cd /home/exedev/watcher/.worktrees/feat-138-watcher-phase2b-change-bus && \
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs) && \
  uv run alembic revision --autogenerate -m "add changes outbox columns"
```

Inspect the generated file. Verify it adds two columns to `changes` and nothing else.

- [ ] **Step 3: Apply migration**

```bash
uv run alembic upgrade head
```

- [ ] **Step 4: Smoke test the column round-trip**

Add a test at `tests/core/models/test_change_outbox.py` (create if needed):
```python
"""Outbox column round-trip on the Change model."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.models.change import Change


@pytest.mark.asyncio
async def test_outbox_columns_default_null(session, watch_factory, snapshot_factory):
    watch = await watch_factory()
    snap1 = await snapshot_factory(watch_id=watch.id)
    snap2 = await snapshot_factory(watch_id=watch.id)
    change = Change(
        watch_id=watch.id, previous_snapshot_id=snap1.id, current_snapshot_id=snap2.id
    )
    session.add(change)
    await session.commit()
    fetched = (await session.execute(select(Change).where(Change.id == change.id))).scalar_one()
    assert fetched.published_to_bus_at is None
    assert fetched.bus_message_id is None


@pytest.mark.asyncio
async def test_outbox_columns_round_trip(session, watch_factory, snapshot_factory):
    watch = await watch_factory()
    snap1 = await snapshot_factory(watch_id=watch.id)
    snap2 = await snapshot_factory(watch_id=watch.id)
    now = datetime.now(UTC)
    change = Change(
        watch_id=watch.id,
        previous_snapshot_id=snap1.id,
        current_snapshot_id=snap2.id,
        published_to_bus_at=now,
        bus_message_id="1234567-0",
    )
    session.add(change)
    await session.commit()
    fetched = (await session.execute(select(Change).where(Change.id == change.id))).scalar_one()
    assert fetched.published_to_bus_at == now
    assert fetched.bus_message_id == "1234567-0"
```

If `watch_factory` and `snapshot_factory` fixtures don't exist in `tests/conftest.py`, look at how existing change tests construct Change rows (e.g., `tests/core/models/test_change.py` if any exists, or `tests/api/test_changes.py`). Mirror that pattern. If those fixtures don't exist either, construct the prerequisite Watch + Snapshot rows inline.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/core/models/test_change_outbox.py --no-cov -v
```

Expected: 2 passed.

- [ ] **Step 6: Run the full Watcher suite to confirm no regressions**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -3
```

Expected: 689 passed (or 687 + 2 new). No regressions.

- [ ] **Step 7: Commit**

```bash
git add src/core/models/change.py alembic/versions tests/core/models/test_change_outbox.py
git commit -m "#138 feat: add outbox columns to changes table"
```

---

## Task 3: ChangePublisher class

**Files:**
- Create: `src/core/changes/publisher.py`
- Create: `tests/core/changes/test_publisher.py`

**Public API (per design doc):**
```python
publisher.publish_change(topic, key, payload, headers) -> message_id
```

- [ ] **Step 1: Write the failing tests first (TDD)**

`tests/core/changes/test_publisher.py`:
```python
"""ChangePublisher tests using fakeredis (no real Redis required)."""

import pytest
from fakeredis import aioredis as fakeredis_aio

from src.core.changes.publisher import ChangePublisher


@pytest.fixture
async def fake_redis():
    client = fakeredis_aio.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def publisher(fake_redis):
    p = ChangePublisher(redis_client=fake_redis)
    yield p


@pytest.mark.asyncio
async def test_publish_writes_to_stream(publisher, fake_redis):
    msg_id = await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b'{"ok": true}',
        headers={"event_type": "fingerprint_shift"},
    )
    assert msg_id is not None
    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_publish_partition_key_recorded(publisher, fake_redis):
    msg_id = await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b'{"hello": "world"}',
        headers={},
    )
    entries = await fake_redis.xrange("info.changes")
    fields = entries[0][1]
    assert fields[b"key"] == b"01HZZ00000000000000000000A"
    assert fields[b"payload"] == b'{"hello": "world"}'


@pytest.mark.asyncio
async def test_publish_returns_message_id_format(publisher):
    msg_id = await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b"x",
        headers={},
    )
    # Redis Streams IDs are <ms>-<seq>
    assert "-" in msg_id


@pytest.mark.asyncio
async def test_publish_includes_headers_as_separate_fields(publisher, fake_redis):
    await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b"x",
        headers={"event_type": "spec.healed_via_fallback", "schema_version": "1"},
    )
    entries = await fake_redis.xrange("info.changes")
    fields = entries[0][1]
    assert fields[b"hdr.event_type"] == b"spec.healed_via_fallback"
    assert fields[b"hdr.schema_version"] == b"1"


@pytest.mark.asyncio
async def test_topic_isolation(publisher, fake_redis):
    await publisher.publish_change(topic="info.changes", key="x", payload=b"a", headers={})
    await publisher.publish_change(topic="info.spec_changes", key="y", payload=b"b", headers={})

    changes = await fake_redis.xrange("info.changes")
    spec_changes = await fake_redis.xrange("info.spec_changes")
    assert len(changes) == 1
    assert len(spec_changes) == 1
```

- [ ] **Step 2: Run; expect failure (no module yet)**

```bash
uv run pytest tests/core/changes/test_publisher.py --no-cov -v
```

- [ ] **Step 3: Implement `src/core/changes/publisher.py`**

```python
"""ChangePublisher — concrete Redis Streams implementation.

No abstraction layer; if a future broker migration is needed, refactor
at that point with knowledge of operational constraints.

Wire format (per stream entry):
    field "key"                 = partition key (UTF-8 bytes)
    field "payload"             = opaque payload (bytes)
    field "hdr.<header_name>"   = header value (UTF-8 bytes), one field per header

Consumers should ignore unknown `hdr.*` fields — header set is open-ended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.core.changes.redis_url import get_redis_url
from src.core.logging import get_logger

if TYPE_CHECKING:
    import redis.asyncio as redis

logger = get_logger(__name__)


class ChangePublisher:
    """Async publisher of Change records to Redis Streams.

    Construct with an explicit `redis_client` (recommended for tests via
    fakeredis), or with no args to lazily build one from `REDIS_URL`.
    """

    def __init__(self, *, redis_client: "redis.Redis | None" = None) -> None:
        self._client = redis_client
        self._owns_client = redis_client is None

    async def _get_client(self) -> "redis.Redis":
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.from_url(get_redis_url())
        return self._client

    async def publish_change(
        self,
        topic: str,
        key: str,
        payload: bytes,
        headers: dict[str, str],
    ) -> str:
        """Publish a Change to the named Redis Stream.

        Returns the Redis Stream message ID (e.g. ``"1700000000000-0"``).
        """
        client = await self._get_client()
        fields: dict[str | bytes, str | bytes] = {
            "key": key.encode("utf-8"),
            "payload": payload,
        }
        for hdr_name, hdr_value in headers.items():
            fields[f"hdr.{hdr_name}"] = hdr_value.encode("utf-8")
        msg_id_bytes = await client.xadd(topic, fields)
        msg_id = msg_id_bytes.decode("utf-8") if isinstance(msg_id_bytes, bytes) else str(msg_id_bytes)
        logger.info(
            "change published",
            extra={"topic": topic, "key": key, "msg_id": msg_id, "payload_bytes": len(payload)},
        )
        return msg_id

    async def aclose(self) -> None:
        """Close the underlying Redis client if we own it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/core/changes/test_publisher.py --no-cov -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/core/changes/publisher.py tests/core/changes/test_publisher.py
git commit -m "#138 feat: ChangePublisher for Redis Streams (concrete, no abstraction)"
```

---

## Task 4: Outbox helpers

**Files:**
- Create: `src/core/changes/outbox.py`
- Create: `tests/core/changes/test_outbox.py`

The outbox helpers are pure DB operations: select unpublished, mark published. The drain worker (Task 5) composes these with the publisher.

- [ ] **Step 1: Write the helpers**

`src/core/changes/outbox.py`:
```python
"""Outbox helpers for the `changes` table.

A Change is "unpublished" while `published_to_bus_at IS NULL`. The drain
worker selects unpublished rows, hands them to the ChangePublisher, then
marks them published.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.change import Change


async def select_unpublished(
    session: AsyncSession, *, limit: int = 100
) -> list[Change]:
    """Return the oldest unpublished Changes, capped at `limit`."""
    result = await session.execute(
        select(Change)
        .where(Change.published_to_bus_at.is_(None))
        .order_by(Change.detected_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mark_published(
    session: AsyncSession, change_id: ULID, *, bus_message_id: str
) -> None:
    """Mark a Change as published with the broker's message ID."""
    change = await session.get(Change, change_id)
    if change is None:
        return
    change.published_to_bus_at = datetime.now(UTC)
    change.bus_message_id = bus_message_id
```

- [ ] **Step 2: Tests**

`tests/core/changes/test_outbox.py`:
```python
"""Outbox helper tests against the real `changes` table."""

import pytest

from src.core.changes.outbox import mark_published, select_unpublished


@pytest.mark.asyncio
async def test_select_unpublished_returns_only_unpublished(session, change_factory):
    c1 = await change_factory()
    c2 = await change_factory()
    await mark_published(session, c1.id, bus_message_id="1-0")
    await session.commit()

    unpublished = await select_unpublished(session)
    assert len(unpublished) == 1
    assert unpublished[0].id == c2.id


@pytest.mark.asyncio
async def test_select_unpublished_orders_by_detected_at(session, change_factory):
    """Older unpublished rows come first."""
    c_old = await change_factory()
    c_new = await change_factory()

    unpublished = await select_unpublished(session)
    assert [c.id for c in unpublished] == [c_old.id, c_new.id]


@pytest.mark.asyncio
async def test_mark_published_sets_fields(session, change_factory):
    c = await change_factory()
    await mark_published(session, c.id, bus_message_id="abc-0")
    await session.commit()
    await session.refresh(c)
    assert c.bus_message_id == "abc-0"
    assert c.published_to_bus_at is not None


@pytest.mark.asyncio
async def test_mark_published_unknown_id_is_noop(session):
    from ulid import ULID
    # Should not raise.
    await mark_published(session, ULID(), bus_message_id="x")
    await session.commit()


@pytest.mark.asyncio
async def test_select_unpublished_respects_limit(session, change_factory):
    for _ in range(5):
        await change_factory()
    out = await select_unpublished(session, limit=3)
    assert len(out) == 3
```

`change_factory` fixture must produce a Change row with all required FKs (Watch + 2 Snapshots). If no such fixture exists in `tests/conftest.py`, add one or extend an existing factory pattern. Look at `tests/api/test_changes.py` or similar for the existing pattern; reuse it.

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/core/changes/test_outbox.py --no-cov -v
```

Expected: 5 passed.

- [ ] **Step 4: Commit**

```bash
git add src/core/changes/outbox.py tests/core/changes/test_outbox.py tests/conftest.py
git commit -m "#138 feat: outbox select/mark helpers for Change rows"
```

---

## Task 5: Procrastinate drain worker

**Files:**
- Create: `src/workers/changes_drain.py`
- Create: `tests/workers/test_changes_drain.py`

The drain worker iterates unpublished Changes, builds a generic envelope from each row, calls `ChangePublisher.publish_change`, and marks the row published. Payload shape is generic for 2b — Phase 2c will refine when info_item_id/info_spec_id are wired into the Watch model.

- [ ] **Step 1: Implement the drain worker**

`src/workers/changes_drain.py`:
```python
"""Drain unpublished Changes from the outbox to the Redis bus.

Phase 2b ships a generic envelope built from existing Change row fields.
Phase 2c will refine the payload with info_item_id, info_spec_id, fingerprints.
"""

import json

from src.core.changes.outbox import mark_published, select_unpublished
from src.core.changes.publisher import ChangePublisher
from src.core.database import get_session_factory
from src.core.logging import get_logger
from src.core.utils import format_utc_iso
from src.workers import bp

logger = get_logger(__name__)

INFO_CHANGES_TOPIC = "info.changes"


async def _build_envelope(change) -> bytes:
    """Build the JSON wire envelope for a Change row.

    Phase 2b shape (generic):
      {
        "change_id": "<ULID>",
        "watch_id": "<ULID>",
        "previous_snapshot_id": "<ULID>",
        "current_snapshot_id": "<ULID>",
        "detected_at": "<ISO8601 UTC>",
        "significance": <float | null>,
        "visual_change_score": <float | null>,
        "metadata": <dict>
      }
    Phase 2c will add info_item_id and info_spec_id.
    """
    return json.dumps(
        {
            "change_id": str(change.id),
            "watch_id": str(change.watch_id),
            "previous_snapshot_id": str(change.previous_snapshot_id),
            "current_snapshot_id": str(change.current_snapshot_id),
            "detected_at": format_utc_iso(change.detected_at),
            "significance": change.significance,
            "visual_change_score": change.visual_change_score,
            "metadata": change.change_metadata,
        }
    ).encode("utf-8")


@bp.task(name="drain_changes_outbox", queue="default")
async def drain_changes_outbox(*, batch_size: int = 100) -> dict:
    """Publish up to `batch_size` unpublished Changes; return counts.

    Idempotent — only processes rows where `published_to_bus_at IS NULL`.
    Errors on individual rows abort the rest of the batch (next run picks them up).
    """
    publisher = ChangePublisher()
    published = 0
    failed = 0
    try:
        async with get_session_factory()() as session:
            rows = await select_unpublished(session, limit=batch_size)
            for change in rows:
                try:
                    payload = await _build_envelope(change)
                    msg_id = await publisher.publish_change(
                        topic=INFO_CHANGES_TOPIC,
                        # Phase 2b uses watch_id as partition key; 2c switches to info_item_id.
                        key=str(change.watch_id),
                        payload=payload,
                        headers={"schema_version": "1"},
                    )
                    await mark_published(session, change.id, bus_message_id=msg_id)
                    published += 1
                except Exception as e:
                    logger.exception(
                        "change drain failed for row",
                        extra={"change_id": str(change.id), "error": str(e)},
                    )
                    failed += 1
            await session.commit()
    finally:
        await publisher.aclose()
    logger.info("drain_changes_outbox finished", extra={"published": published, "failed": failed})
    return {"published": published, "failed": failed}
```

- [ ] **Step 2: Tests**

`tests/workers/test_changes_drain.py`:
```python
"""End-to-end drain worker tests with fakeredis + test DB."""

import json
from unittest.mock import patch

import pytest
from fakeredis import aioredis as fakeredis_aio

from src.core.changes.publisher import ChangePublisher
from src.workers.changes_drain import drain_changes_outbox


@pytest.fixture
async def fake_redis():
    client = fakeredis_aio.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def publisher_uses_fake_redis(fake_redis):
    """Patch ChangePublisher to use the fakeredis client."""
    original_init = ChangePublisher.__init__

    def patched_init(self, *, redis_client=None):
        original_init(self, redis_client=fake_redis)

    with patch.object(ChangePublisher, "__init__", patched_init):
        yield


@pytest.mark.asyncio
async def test_drain_publishes_unpublished(
    session, change_factory, fake_redis, publisher_uses_fake_redis
):
    c1 = await change_factory()
    c2 = await change_factory()
    await session.commit()

    result = await drain_changes_outbox()
    assert result["published"] == 2
    assert result["failed"] == 0

    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 2
    payload_change_ids = {json.loads(e[1][b"payload"])["change_id"] for e in entries}
    assert payload_change_ids == {str(c1.id), str(c2.id)}


@pytest.mark.asyncio
async def test_drain_marks_rows_published(
    session, change_factory, fake_redis, publisher_uses_fake_redis
):
    c = await change_factory()
    await session.commit()

    await drain_changes_outbox()

    await session.refresh(c)
    assert c.published_to_bus_at is not None
    assert c.bus_message_id is not None


@pytest.mark.asyncio
async def test_drain_skips_already_published(
    session, change_factory, fake_redis, publisher_uses_fake_redis
):
    c = await change_factory()
    await session.commit()

    await drain_changes_outbox()
    result = await drain_changes_outbox()  # second call

    assert result["published"] == 0
    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1
```

The `change_factory` fixture lives in `tests/conftest.py` (added in Task 4 if not present).

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/workers/test_changes_drain.py --no-cov -v
```

Expected: 3 passed.

- [ ] **Step 4: Run full Watcher suite**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -3
```

Expected: 697 passed (or whatever — confirm no regressions).

- [ ] **Step 5: Commit**

```bash
git add src/workers/changes_drain.py tests/workers/test_changes_drain.py
git commit -m "#138 feat: drain_changes_outbox Procrastinate task"
```

---

## Task 6: Reference consumer at `tools/info_changes_consumer.py`

**Files:**
- Create: `tools/__init__.py` (if not present)
- Create: `tools/info_changes_consumer.py`
- Create: `tests/tools/__init__.py` (if not present)
- Create: `tests/tools/test_info_changes_consumer.py`

This consumer proves the contract end-to-end: it XREADGROUPs from `info.changes`, validates the envelope shape, and writes events to a JSONL file. Useful as both a smoke test and a template for the future Archive service consumer.

- [ ] **Step 1: Implement the consumer**

`tools/info_changes_consumer.py`:
```python
"""Reference consumer for `info.changes` — XREADGROUP loop with JSONL output.

Usage:
    uv run python tools/info_changes_consumer.py --group archive-ref --output /tmp/info-changes.jsonl

Run alongside Watcher to verify the wire end-to-end. Acks each message after
writing it to the output file. On startup, creates the consumer group if it
doesn't exist (MKSTREAM ensures the stream exists).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import redis.asyncio as redis

from src.core.changes.redis_url import get_redis_url

DEFAULT_TOPIC = "info.changes"
DEFAULT_GROUP = "reference-consumer"


async def _ensure_group(client: redis.Redis, topic: str, group: str) -> None:
    try:
        await client.xgroup_create(name=topic, groupname=group, id="$", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def consume(
    *,
    topic: str,
    group: str,
    consumer_name: str,
    output: Path,
    block_ms: int = 5000,
    max_messages: int | None = None,
) -> int:
    """Run the consume loop. Returns count of messages processed."""
    client = redis.from_url(get_redis_url())
    await _ensure_group(client, topic, group)
    processed = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("a", encoding="utf-8") as fp:
            while True:
                if max_messages is not None and processed >= max_messages:
                    break
                response = await client.xreadgroup(
                    groupname=group,
                    consumername=consumer_name,
                    streams={topic: ">"},
                    count=10,
                    block=block_ms,
                )
                if not response:
                    continue
                for _stream, entries in response:
                    for msg_id, fields in entries:
                        record = _format(msg_id, fields)
                        fp.write(json.dumps(record) + "\n")
                        fp.flush()
                        await client.xack(topic, group, msg_id)
                        processed += 1
                        if max_messages is not None and processed >= max_messages:
                            break
                    if max_messages is not None and processed >= max_messages:
                        break
    finally:
        await client.aclose()
    return processed


def _format(msg_id: bytes, fields: dict[bytes, bytes]) -> dict:
    out: dict = {"_msg_id": msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)}
    if b"key" in fields:
        out["key"] = fields[b"key"].decode("utf-8")
    if b"payload" in fields:
        try:
            out["payload"] = json.loads(fields[b"payload"].decode("utf-8"))
        except json.JSONDecodeError:
            out["payload"] = fields[b"payload"].decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    for k, v in fields.items():
        key = k.decode("utf-8") if isinstance(k, bytes) else str(k)
        if key.startswith("hdr."):
            headers[key[4:]] = v.decode("utf-8") if isinstance(v, bytes) else str(v)
    if headers:
        out["headers"] = headers
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Reference consumer for info.changes")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--consumer", default="ref-1")
    parser.add_argument("--output", type=Path, default=Path("/tmp/info-changes.jsonl"))
    parser.add_argument("--max-messages", type=int, default=None,
                        help="Exit after this many messages (default: run forever)")
    args = parser.parse_args()
    processed = asyncio.run(
        consume(
            topic=args.topic,
            group=args.group,
            consumer_name=args.consumer,
            output=args.output,
            max_messages=args.max_messages,
        )
    )
    print(f"Processed {processed} messages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Tests with fakeredis**

`tests/tools/test_info_changes_consumer.py`:
```python
"""Reference consumer tests against fakeredis."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fakeredis import aioredis as fakeredis_aio

import tools.info_changes_consumer as consumer


@pytest.fixture
async def fake_redis():
    client = fakeredis_aio.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def patch_redis_from_url(fake_redis):
    with patch.object(consumer.redis, "from_url", return_value=fake_redis):
        yield


@pytest.mark.asyncio
async def test_consume_reads_and_writes_jsonl(fake_redis, patch_redis_from_url, tmp_path):
    # Seed two messages.
    await fake_redis.xadd("info.changes", {"key": "X", "payload": b'{"a": 1}'})
    await fake_redis.xadd("info.changes", {"key": "Y", "payload": b'{"b": 2}'})

    out_file = tmp_path / "info-changes.jsonl"
    processed = await consumer.consume(
        topic="info.changes",
        group="ref-test",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=2,
    )
    assert processed == 2
    lines = out_file.read_text().strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert {r["payload"]["a"] if "a" in r["payload"] else r["payload"]["b"] for r in records} == {1, 2}


@pytest.mark.asyncio
async def test_consume_creates_group_idempotently(fake_redis, patch_redis_from_url, tmp_path):
    out_file = tmp_path / "out.jsonl"
    # First call creates the group.
    await consumer.consume(
        topic="info.changes",
        group="g1",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=0,
    )
    # Second call must not error on BUSYGROUP.
    await consumer.consume(
        topic="info.changes",
        group="g1",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=0,
    )


@pytest.mark.asyncio
async def test_consume_acks_messages(fake_redis, patch_redis_from_url, tmp_path):
    await fake_redis.xadd("info.changes", {"key": "X", "payload": b'{"a": 1}'})
    out_file = tmp_path / "out.jsonl"
    await consumer.consume(
        topic="info.changes",
        group="g-ack",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    # Verify pending entries list (PEL) is empty for this consumer.
    pending = await fake_redis.xpending("info.changes", "g-ack")
    assert pending["pending"] == 0
```

- [ ] **Step 3: Run consumer tests**

```bash
uv run pytest tests/tools/test_info_changes_consumer.py --no-cov -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/info_changes_consumer.py tests/tools/test_info_changes_consumer.py tools/__init__.py tests/tools/__init__.py
git commit -m "#138 feat: reference consumer for info.changes (XREADGROUP → JSONL)"
```

---

## Task 7: Documentation + AGENTS update

**Files:**
- Modify: `AGENTS.md` (Environment Files section — add `REDIS_URL`)
- Modify: `docs/COMMANDS.md` (add a "Change bus" section)

- [ ] **Step 1: Update AGENTS.md Environment Files section**

In the Environment Files section's variables list (or wherever environment variables are documented), add:
- `REDIS_URL` — Redis connection URL (default: `redis://localhost:6379/0`). Used by `ChangePublisher` and `tools/info_changes_consumer.py`.

- [ ] **Step 2: Update docs/COMMANDS.md**

Add a new section after "Database":

```markdown
## Change bus (Redis Streams)

```bash
# Run the reference consumer (requires Redis running on REDIS_URL):
uv run python tools/info_changes_consumer.py --group archive-ref --output /tmp/info-changes.jsonl

# Inspect a stream's contents quickly:
redis-cli XLEN info.changes
redis-cli XRANGE info.changes - +

# Drain unpublished Changes manually (Procrastinate task):
uv run python -c "import asyncio; from src.workers.changes_drain import drain_changes_outbox; print(asyncio.run(drain_changes_outbox.func()))"
```
```

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md docs/COMMANDS.md
git commit -m "#138 docs: REDIS_URL env var + change-bus command reference"
```

---

## Task 8: Smoke test against real Redis

**Files:** none modified.

This task verifies the wire works end-to-end against actual Redis (not fakeredis). Requires the user to have run the install command from Pre-flight.

- [ ] **Step 1: Verify Redis is up**

```bash
redis-cli ping
```

Expected: `PONG`. If not, ask the user to install/start Redis per Pre-flight.

- [ ] **Step 2: Publish a synthetic Change via the publisher**

```bash
cd /home/exedev/watcher/.worktrees/feat-138-watcher-phase2b-change-bus && \
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs) && \
  uv run python -c "
import asyncio
from src.core.changes.publisher import ChangePublisher

async def main():
    p = ChangePublisher()
    msg_id = await p.publish_change(
        topic='info.changes',
        key='test-key',
        payload=b'{\"smoke\": true}',
        headers={'event_type': 'smoke'},
    )
    print(f'Published: {msg_id}')
    await p.aclose()

asyncio.run(main())
"
```

Expected: prints `Published: <ms>-<seq>`.

```bash
redis-cli XLEN info.changes
```

Expected: ≥1.

- [ ] **Step 3: Run the reference consumer briefly**

```bash
uv run python tools/info_changes_consumer.py --max-messages 1 --output /tmp/info-changes-smoke.jsonl
```

Expected: prints `Processed 1 messages` and writes one JSON line to the output file.

```bash
cat /tmp/info-changes-smoke.jsonl
```

Verify the JSON line has the expected fields: `_msg_id`, `key`, `payload` (with the smoke marker), and `headers.event_type == "smoke"`.

- [ ] **Step 4: Tear down stream + cleanup**

```bash
redis-cli DEL info.changes
rm /tmp/info-changes-smoke.jsonl
```

- [ ] **Step 5: Final test suite + lint**

```bash
cd /home/exedev/watcher/.worktrees/feat-138-watcher-phase2b-change-bus && \
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs) && \
  uv run pytest --no-cov -m "not integration" 2>&1 | tail -3 && \
  uv run ruff check src/core/changes src/workers/changes_drain.py tools/ tests/core/changes tests/tools tests/workers/test_changes_drain.py
```

Expected: all tests pass, ruff clean.

- [ ] **Step 6: Push branch**

(Skip — controller will merge directly to main after review.)

---

## Wrap-up

After Task 8:
- Redis is running on the VM and `ChangePublisher` writes to `info.changes` stream.
- Outbox columns added to `changes` table; drain worker is registered as a Procrastinate task.
- Reference consumer at `tools/info_changes_consumer.py` reads from `info.changes` and writes JSONL.
- All wire-format conventions documented in `publisher.py` docstring (`key`, `payload`, `hdr.*` field naming).
- Phase 2c can wire actual fingerprint-shift detection to the outbox + drain.

**Out of scope for 2b** (deferred to 2c):
- Refining the payload schema with `info_item_id`, `info_spec_id`, fingerprint values
- Switching the partition key from `watch_id` to `info_item_id`
- Triggering `drain_changes_outbox` on a Procrastinate cron schedule
- Wiring SDK `InformationClient` into the Watch creation flow
