"""Restoring a shipped dump (#296 D8).

The restore is also the migration's transfer path (#296 D10): the cutover dump
moves through the bucket, so the procedure an incident would need is the one
the move exercises on real data.
"""

import hashlib
import logging
import os
import re
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from src.ops import restore
from src.ops.backup import BUCKET_ENV, KEY_TIME_FORMAT, PREFIX_ENV
from src.ops.restore import RestoreError
from tests.ops.gcs_fakes import FakeBucket, FakeClient
from tests.ops.test_backup import DUMP_BYTES, FakePg

BUCKET = "co-gcs-watcher-backup"


def _ship(
    bucket: FakeBucket,
    key: str,
    data: bytes = DUMP_BYTES,
    *,
    created: datetime | None = None,
    **meta,
) -> None:
    """Put an object where the backup would, with the digest it records.

    Created a minute after the time its name claims, as an honest upload is —
    unless ``created`` says otherwise (a forged, future-dated name).
    """
    bucket.objects[key] = data
    bucket.metadata[key] = {"sha256": hashlib.sha256(data).hexdigest(), **meta}
    if created is None:
        match = re.search(r"(\d{8}T\d{6}Z)\.dump$", key)
        stamp = datetime.strptime(match.group(1), KEY_TIME_FORMAT) if match else datetime.now()
        created = stamp.replace(tzinfo=UTC) + timedelta(minutes=1)
    bucket.created[key] = created


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
        assert [snapshot.name for snapshot in listed] == [
            "co-watcher/20260911T031702Z.dump",
            "co-watcher/20260912T031702Z.dump",
        ]
        assert listed[1].metadata["alembic_head"] == "b"

    def test_latest_is_the_newest_name(self, client, bucket) -> None:
        _ship(bucket, "co-watcher/20260911T031702Z.dump")
        _ship(bucket, "co-watcher/20260912T031702Z.dump")
        assert (
            restore.latest_key(client, BUCKET, "co-watcher") == "co-watcher/20260912T031702Z.dump"
        )

    def test_no_snapshot_is_an_error(self, client) -> None:
        with pytest.raises(RestoreError, match="no dumps"):
            restore.latest_key(client, BUCKET, "co-watcher")

    def test_latest_passes_over_a_name_later_than_its_upload(self, client, bucket, caplog) -> None:
        """A name is the writer's claim; the creation time is the bucket's. An
        honest dump is named before it is uploaded, so a name later than its
        object's creation is a skewed clock or a forgery — and a compromised
        writer planting ``2099…`` would otherwise own ``--latest`` for the 30
        days the lifecycle rule takes to remove it."""
        _ship(bucket, "watcher/20260911T031702Z.dump")
        _ship(
            bucket,
            "watcher/20990101T000000Z.dump",
            created=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        )
        with caplog.at_level(logging.WARNING, logger="src.ops.restore"):
            assert restore.latest_key(client, BUCKET, "watcher") == "watcher/20260911T031702Z.dump"
        assert any("20990101T000000Z" in r.getMessage() for r in caplog.records)

    def test_a_name_within_clock_skew_of_its_upload_is_still_a_candidate(
        self, client, bucket
    ) -> None:
        """The dumping host's clock and the bucket's are not the same clock."""
        _ship(
            bucket,
            "watcher/20260911T031702Z.dump",
            created=datetime(2026, 9, 11, 3, 16, 30, tzinfo=UTC),
        )
        assert restore.latest_key(client, BUCKET, "watcher") == "watcher/20260911T031702Z.dump"

    def test_only_suspect_names_is_no_dump_at_all(self, client, bucket) -> None:
        _ship(
            bucket,
            "watcher/20990101T000000Z.dump",
            created=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        )
        with pytest.raises(RestoreError, match="no dumps"):
            restore.latest_key(client, BUCKET, "watcher")


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


class TestFetchIsPrivate:
    """A fetched dump is the whole production database, fetched as root.

    sudo's umask is 0022, so a plain ``mkdir`` and the SDK's plain ``open``
    left a 0644 dump in a 0755 directory — and a directory someone else made
    first under ``/tmp`` let them plant a symlink for root to write through.
    """

    @pytest.fixture(autouse=True)
    def _sudo_umask(self):
        previous = os.umask(0o022)
        yield
        os.umask(previous)

    def test_the_directory_and_the_dump_are_private(self, client, bucket, tmp_path) -> None:
        _ship(bucket, "watcher/x.dump")
        dest = tmp_path / "fetched"
        path = restore.fetch(client, BUCKET, "watcher/x.dump", dest, runner=FakePg())
        assert stat.S_IMODE(dest.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_directory_others_can_enter_is_refused(self, client, bucket, tmp_path) -> None:
        _ship(bucket, "watcher/x.dump")
        dest = tmp_path / "shared"
        dest.mkdir(mode=0o755)
        with pytest.raises(RestoreError, match="private"):
            restore.fetch(client, BUCKET, "watcher/x.dump", dest, runner=FakePg())
        assert list(dest.iterdir()) == []

    def test_a_directory_someone_else_owns_is_refused(
        self, client, bucket, tmp_path, monkeypatch
    ) -> None:
        _ship(bucket, "watcher/x.dump")
        dest = tmp_path / "theirs"
        dest.mkdir(mode=0o700)
        someone_else = os.geteuid() + 1
        monkeypatch.setattr(restore.os, "geteuid", lambda: someone_else)
        with pytest.raises(RestoreError, match="owned"):
            restore.fetch(client, BUCKET, "watcher/x.dump", dest, runner=FakePg())

    def test_a_planted_link_is_not_written_through(self, client, bucket, tmp_path) -> None:
        _ship(bucket, "watcher/x.dump")
        dest = tmp_path / "fetched"
        dest.mkdir(mode=0o700)
        victim = tmp_path / "victim"
        victim.write_bytes(b"precious")
        (dest / "x.dump").symlink_to(victim)
        with pytest.raises(RestoreError, match="exists"):
            restore.fetch(client, BUCKET, "watcher/x.dump", dest, runner=FakePg())
        assert victim.read_bytes() == b"precious"


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
        assert seen["argv"][:6] == [
            "setpriv",
            "--reuid=postgres",
            "--regid=postgres",
            "--init-groups",
            "--reset-env",
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

    def test_list_flags_a_name_later_than_its_upload(self, wired, bucket, capsys) -> None:
        _ship(bucket, "watcher/20260911T031702Z.dump")
        _ship(
            bucket,
            "watcher/20990101T000000Z.dump",
            created=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        )
        assert restore.main(["--list", "--prefix", "watcher"]) == 0
        lines = capsys.readouterr().out.splitlines()
        (forged,) = [line for line in lines if "20990101T000000Z" in line]
        (honest,) = [line for line in lines if "20260911T031702Z" in line]
        assert "SUSPECT" in forged
        assert "SUSPECT" not in honest

    def test_an_empty_listing_says_so(self, wired, bucket, capsys) -> None:
        _ship(bucket, "watcher/20260911T031702Z.dump")
        assert restore.main(["--list", "--prefix", "co-watcher"]) == 0
        assert "no dumps under gs://co-gcs-watcher-backup/co-watcher/" in capsys.readouterr().err
