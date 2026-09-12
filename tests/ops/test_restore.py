"""Restoring a shipped dump (#296 D8).

The restore is also the migration's transfer path (#296 D10): the cutover dump
moves through the bucket, so the procedure an incident would need is the one
the move exercises on real data.
"""

import hashlib
import subprocess

import pytest

from src.ops import restore
from src.ops.backup import BUCKET_ENV, PREFIX_ENV
from src.ops.restore import RestoreError
from tests.ops.gcs_fakes import FakeBucket, FakeClient
from tests.ops.test_backup import DUMP_BYTES, FakePg

BUCKET = "co-gcs-watcher-backup"


def _ship(bucket: FakeBucket, key: str, data: bytes = DUMP_BYTES, **meta) -> None:
    """Put an object where the backup would, with the digest it records."""
    bucket.objects[key] = data
    bucket.metadata[key] = {"sha256": hashlib.sha256(data).hexdigest(), **meta}


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket(BUCKET)


@pytest.fixture
def client(bucket) -> FakeClient:
    return FakeClient(bucket)


class TestFindingASnapshot:
    def test_list_reads_names_and_metadata_in_time_order(self, client, bucket) -> None:
        _ship(bucket, "co-watcher/20260912T031702Z.dump", alembic_head="b")
        _ship(bucket, "co-watcher/20260911T031702Z.dump", alembic_head="a")
        _ship(bucket, "watcher/20260910T000000Z.dump")
        listed = restore.list_snapshots(client, BUCKET, "co-watcher")
        assert [name for name, _ in listed] == [
            "co-watcher/20260911T031702Z.dump",
            "co-watcher/20260912T031702Z.dump",
        ]
        assert listed[1][1]["alembic_head"] == "b"

    def test_latest_is_the_newest_name(self, client, bucket) -> None:
        _ship(bucket, "co-watcher/20260911T031702Z.dump")
        _ship(bucket, "co-watcher/20260912T031702Z.dump")
        assert (
            restore.latest_key(client, BUCKET, "co-watcher") == "co-watcher/20260912T031702Z.dump"
        )

    def test_no_snapshot_is_an_error(self, client) -> None:
        with pytest.raises(RestoreError, match="no dumps"):
            restore.latest_key(client, BUCKET, "co-watcher")


class TestFetch:
    def test_downloads_and_verifies(self, client, bucket, tmp_path) -> None:
        _ship(bucket, "co-watcher/x.dump")
        path = restore.fetch(client, BUCKET, "co-watcher/x.dump", tmp_path, runner=FakePg())
        assert path.read_bytes() == DUMP_BYTES

    def test_a_download_that_does_not_match_its_recorded_digest_is_refused(
        self, client, bucket, tmp_path
    ) -> None:
        """What the backup recorded is what must come back; anything else is
        corruption or tampering, and it is not restored."""
        _ship(bucket, "co-watcher/x.dump")
        bucket.objects["co-watcher/x.dump"] = DUMP_BYTES + b"tampered"
        with pytest.raises(RestoreError, match="sha256"):
            restore.fetch(client, BUCKET, "co-watcher/x.dump", tmp_path, runner=FakePg())

    def test_a_dump_without_a_recorded_digest_is_refused(self, client, bucket, tmp_path) -> None:
        bucket.objects["co-watcher/x.dump"] = DUMP_BYTES
        with pytest.raises(RestoreError, match="sha256"):
            restore.fetch(client, BUCKET, "co-watcher/x.dump", tmp_path, runner=FakePg())

    def test_a_missing_object_is_an_error(self, client, tmp_path) -> None:
        with pytest.raises(RestoreError, match="not found"):
            restore.fetch(client, BUCKET, "co-watcher/nope.dump", tmp_path, runner=FakePg())

    def test_an_archive_pg_restore_cannot_read_is_refused(self, client, bucket, tmp_path) -> None:
        _ship(bucket, "co-watcher/x.dump")
        with pytest.raises(RestoreError, match="pg_restore"):
            restore.fetch(
                client, BUCKET, "co-watcher/x.dump", tmp_path, runner=FakePg(fail="pg_restore")
            )


class TestRestoreInto:
    def test_one_transaction_through_stdin_as_postgres(self, tmp_path) -> None:
        """All or nothing, so a failed restore leaves an empty database rather
        than half of one. Through stdin, so ``postgres`` never needs to read a
        file root wrote."""
        dump = tmp_path / "x.dump"
        dump.write_bytes(DUMP_BYTES)
        seen: dict = {}

        def runner(argv, **kwargs):
            seen["argv"] = argv
            seen["stdin"] = kwargs["stdin"].read()
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        restore.restore_into(dump, "watcher", run_as="postgres", runner=runner)
        assert seen["argv"][:5] == [
            "setpriv",
            "--reuid=postgres",
            "--regid=postgres",
            "--init-groups",
            "--",
        ]
        assert {"--single-transaction", "--exit-on-error", "--dbname=watcher"} <= set(seen["argv"])
        assert seen["stdin"] == DUMP_BYTES

    def test_a_failed_restore_is_an_error(self, tmp_path) -> None:
        dump = tmp_path / "x.dump"
        dump.write_bytes(DUMP_BYTES)
        with pytest.raises(RestoreError, match="pg_restore"):
            restore.restore_into(dump, "watcher", run_as=None, runner=FakePg(fail="pg_restore"))


class TestMain:
    @pytest.fixture
    def wired(self, monkeypatch, client):
        monkeypatch.setattr(restore.storage, "Client", lambda: client)
        monkeypatch.setattr(restore.subprocess, "run", FakePg())
        monkeypatch.setenv(BUCKET_ENV, BUCKET)
        # What a restoring host's own backup.env may hold: it names the host
        # the restore runs on, which is never the one it restores from.
        monkeypatch.setenv(PREFIX_ENV, "co-watcher")

    def test_latest_needs_the_source_host_named(self, wired, bucket, tmp_path, capsys) -> None:
        """A restore runs on another host — the cutover's co-watcher, an
        incident's replacement — so a default prefix is the restoring host's,
        and that is the one prefix never wanted."""
        _ship(bucket, "watcher/20260911T031702Z.dump")
        assert restore.main(["--latest", "--download-only", str(tmp_path / "d")]) == 2
        assert "--prefix" in capsys.readouterr().err

    def test_latest_takes_the_named_hosts_newest_not_this_hosts(
        self, wired, bucket, tmp_path
    ) -> None:
        """Once the new host's own timer has run, its dump is the newest name in
        the bucket — and it passes every check, being a real dump of a real
        (near-empty) database. Only the named source's dumps are candidates."""
        _ship(bucket, "watcher/20260911T031702Z.dump")
        _ship(bucket, "co-watcher/20260912T031702Z.dump", DUMP_BYTES + b"the new host's own")
        dest = tmp_path / "d"
        assert restore.main(["--latest", "--prefix", "watcher", "--download-only", str(dest)]) == 0
        assert [p.name for p in dest.iterdir()] == ["20260911T031702Z.dump"]

    def test_list_without_a_prefix_shows_every_host(self, wired, bucket, capsys) -> None:
        _ship(bucket, "watcher/20260911T031702Z.dump")
        _ship(bucket, "co-watcher/20260912T031702Z.dump")
        assert restore.main(["--list"]) == 0
        out = capsys.readouterr().out
        assert "watcher/20260911T031702Z.dump" in out
        assert "co-watcher/20260912T031702Z.dump" in out

    def test_an_empty_listing_says_so(self, wired, bucket, capsys) -> None:
        _ship(bucket, "watcher/20260911T031702Z.dump")
        assert restore.main(["--list", "--prefix", "co-watcher"]) == 0
        assert "no dumps under gs://co-gcs-watcher-backup/co-watcher/" in capsys.readouterr().err
