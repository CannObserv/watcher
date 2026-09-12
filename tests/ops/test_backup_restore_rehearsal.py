"""A real dump, shipped and restored, on every integration run (#296 D8).

broker#4's rule: an untested backup is a second thing to discover during an
incident, so the round trip runs in the suite, not once by hand. Real
``pg_dump`` / ``pg_restore`` against scratch databases; only the bucket is the
SDK-faithful fake.

The part no fake can answer is **the two-role model** (#259) — the one thing
the cohort's earlier migrations never had to carry. A restore that loses
``watcher_app``'s grants or its default privileges looks healthy until the
first write fails, so this asserts what #296's go/no-go gate asserts: DML
granted, DDL refused, ``alembic_version`` read-only, future tables covered.
"""

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.ops import backup, restore
from tests.conftest import TEST_DATABASE_URL
from tests.ops.gcs_fakes import FakeBucket, FakeClient

pytestmark = pytest.mark.integration

APP_ROLE = "watcher_app"
BUCKET = "rehearsal-bucket"


def _with_database(url: str, name: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", url, count=1)


def _libpq(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _major(version_text: str) -> int:
    return int(re.search(r"(\d+)", version_text).group(1))


@pytest.fixture
async def databases():
    """Two scratch databases on the test server, created and always dropped.

    ``_test``-suffixed so ``db_safety`` would treat them as non-production, and
    named per process so parallel suites cannot collide.
    """
    for binary in ("pg_dump", "pg_restore", "psql"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary} not installed")
    admin = create_async_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    names = (f"watcher_rh{os.getpid()}_src_test", f"watcher_rh{os.getpid()}_dst_test")
    async with admin.connect() as conn:
        server_major = int((await conn.execute(text("SHOW server_version_num"))).scalar()) // 10000
        dump_version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True)
        if _major(dump_version.stdout) < server_major:
            pytest.skip(f"pg_dump is older than the server ({dump_version.stdout.strip()})")
        exists = (
            await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE})
        ).scalar()
        if not exists:
            try:
                await conn.execute(text(f"CREATE ROLE {APP_ROLE} NOLOGIN"))
            except Exception as exc:  # noqa: BLE001 — no CREATEROLE here: nothing to rehearse
                pytest.skip(f"{APP_ROLE} absent and cannot be created: {exc}")
        for name in names:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            await conn.execute(text(f"CREATE DATABASE {name}"))
    try:
        yield tuple(_with_database(TEST_DATABASE_URL, name) for name in names)
    finally:
        async with admin.connect() as conn:
            for name in names:
                await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        await admin.dispose()


SEED = [
    "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)",
    "INSERT INTO alembic_version VALUES ('rehearsal01')",
    "CREATE TABLE watched_items (id bigserial PRIMARY KEY, name text NOT NULL)",
    "INSERT INTO watched_items (name) SELECT 'item ' || g FROM generate_series(1, 25) g",
    # The shape scripts/setup-db-roles.sql leaves behind, schema-level half.
    f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
    f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {APP_ROLE}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}",
]

CHECKS = {
    "items": "SELECT count(*) FROM watched_items",
    "head": "SELECT version_num FROM alembic_version",
    "dml": f"SELECT has_table_privilege('{APP_ROLE}', 'watched_items', 'INSERT')",
    "seq": f"SELECT has_sequence_privilege('{APP_ROLE}', 'watched_items_id_seq', 'USAGE')",
    "head_writable": f"SELECT has_table_privilege('{APP_ROLE}', 'alembic_version', 'INSERT')",
    "ddl": f"SELECT has_schema_privilege('{APP_ROLE}', 'public', 'CREATE')",
    "future_tables": (
        "SELECT count(*) FROM pg_default_acl d "
        "WHERE d.defaclobjtype = 'r' AND array_to_string(d.defaclacl, ',') LIKE :grantee"
    ),
}


async def _read(url: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return {
                name: (await conn.execute(text(sql), {"grantee": f"%{APP_ROLE}=%"})).scalar()
                for name, sql in CHECKS.items()
            }
    finally:
        await engine.dispose()


async def test_a_dump_survives_the_round_trip_with_its_grants(databases, tmp_path: Path) -> None:
    source, target = databases
    engine = create_async_engine(source)
    async with engine.begin() as conn:
        for statement in SEED:
            await conn.execute(text(statement))
    await engine.dispose()

    client = FakeClient(FakeBucket(BUCKET))
    summary = backup.run_backup(
        database=_libpq(source),
        bucket=BUCKET,
        prefix="rehearsal",
        client=client,
        workdir=tmp_path / "backup",
        run_as=None,
        host="rehearsal",
    )
    assert summary["outcome"] == "uploaded"
    assert summary["alembic_head"] == "rehearsal01"

    key = restore.latest_key(client, BUCKET, "rehearsal")
    path = restore.fetch(client, BUCKET, key, tmp_path / "fetched", runner=subprocess.run)
    restore.restore_into(path, _libpq(target), run_as=None, runner=subprocess.run)

    before, after = await _read(source), await _read(target)
    assert after == before
    assert after["items"] == 25
    assert after["head"] == "rehearsal01"
    assert after["dml"] is True
    assert after["seq"] is True
    assert after["head_writable"] is False
    assert after["ddl"] is False
    assert after["future_tables"] >= 1


async def test_a_truncated_dump_is_refused_though_it_still_lists(databases, tmp_path: Path) -> None:
    """The premise, on a real archive: ``pg_restore --list`` passes a dump cut
    short, because the table of contents precedes the data. ``verify_dump``
    must not — it reads every block through."""
    source, _ = databases
    engine = create_async_engine(source)
    async with engine.begin() as conn:
        # Enough rows that the cut lands in the data, well past the TOC.
        for statement in (
            *SEED[:3],
            "INSERT INTO watched_items (name) SELECT md5(g::text) FROM generate_series(1, 20000) g",
        ):
            await conn.execute(text(statement))
    await engine.dispose()

    dump = backup.take_dump(
        _libpq(source),
        tmp_path / "backup",
        run_as=None,
        runner=subprocess.run,
        now=lambda: datetime.now(UTC),
    )
    cut = tmp_path / "cut.dump"
    cut.write_bytes(dump.path.read_bytes()[: dump.size_bytes * 9 // 10])

    listed = subprocess.run(["pg_restore", "--list", str(cut)], capture_output=True, check=False)
    assert listed.returncode == 0, "premise: a truncated archive still lists"
    with pytest.raises(backup.BackupError, match="end of file"):
        backup.verify_dump(cut, runner=subprocess.run)


async def test_a_restore_into_a_populated_database_changes_nothing(
    databases, tmp_path: Path
) -> None:
    """One transaction: a restore that fails partway leaves the target exactly
    as it was, never half-loaded."""
    source, target = databases
    for url in (source, target):
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            for statement in SEED[:4]:
                await conn.execute(text(statement))
        await engine.dispose()

    client = FakeClient(FakeBucket(BUCKET))
    backup.run_backup(
        database=_libpq(source),
        bucket=BUCKET,
        prefix="rehearsal",
        client=client,
        workdir=tmp_path / "backup",
        run_as=None,
        host="rehearsal",
    )
    path = restore.fetch(
        client,
        BUCKET,
        restore.latest_key(client, BUCKET, "rehearsal"),
        tmp_path / "fetched",
        runner=subprocess.run,
    )
    with pytest.raises(restore.RestoreError):
        restore.restore_into(path, _libpq(target), run_as=None, runner=subprocess.run)

    engine = create_async_engine(target)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM watched_items"))).scalar() == 25
    await engine.dispose()
