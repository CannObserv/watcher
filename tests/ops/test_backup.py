"""The nightly database backup (#296 D8), on broker#4's pattern.

Every decision runs against fakes — which key, which precondition, what the
job refuses to ship, when it checks in. What a fake cannot answer (whether the
SDK accepts these arguments, whether the unit's sandbox reaches the bucket, and
whether a real ``pg_dump`` round-trips the two-role model) is answered by
``test_backup_restore_rehearsal.py`` against a real server, and by the first
real run recorded in docs/RECOVERY.md.
"""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.ops import backup, checkin
from src.ops.backup import BackupError
from tests.ops.gcs_fakes import FakeBucket, FakeClient

BUCKET = "co-gcs-watcher-backup"
NOW = datetime(2026, 9, 12, 3, 17, 2, 512000, tzinfo=UTC)
DUMP_BYTES = b"PGDMP\x01\x0f\x00" + b"x" * 64

#: ``pg_restore --list`` on a real custom-format dump, header verbatim (the
#: entries trimmed to the ones the check reads).
TOC = """\
;
; Archive created at 2026-09-12 03:17:02 UTC
;     dbname: watcher
;     TOC Entries: 312
;     Compression: gzip
;     Dump Version: 1.15-0
;     Format: CUSTOM
;     Integer: 4 bytes
;     Offset: 8 bytes
;     Dumped from database version: 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)
;     Dumped by pg_dump version: 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)
;
;
; Selected TOC Entries:
;
6; 2615 1523230 SCHEMA - public watcher
3549; 0 1523231 TABLE DATA public alembic_version watcher
3551; 0 1523300 TABLE DATA public watched_items watcher
3560; 0 1523400 TABLE DATA public procrastinate_jobs watcher
"""


class FakePg:
    """Answers pg_dump, psql and pg_restore the way the real ones do."""

    def __init__(self, *, toc: str = TOC, head: str = "2f8bb8f7100a", fail: str = "") -> None:
        self.toc = toc
        self.head = head
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        program = argv[argv.index("--") + 1] if argv[0] == "setpriv" else argv[0]
        if program == self.fail:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"{program}: boom\n")
        if program == "pg_dump":
            kwargs["stdout"].write(DUMP_BYTES)
            return subprocess.CompletedProcess(argv, 0, stdout=None, stderr=b"")
        if program == "psql":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.head}\n", stderr="")
        if program == "pg_restore":
            return subprocess.CompletedProcess(argv, 0, stdout=self.toc, stderr="")
        raise AssertionError(f"unexpected program {program}")


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket(BUCKET)


@pytest.fixture
def client(bucket) -> FakeClient:
    return FakeClient(bucket)


def _run(client, tmp_path: Path, **overrides) -> dict:
    kwargs = dict(
        database="watcher",
        bucket=BUCKET,
        prefix="co-watcher",
        client=client,
        workdir=tmp_path,
        run_as="postgres",
        runner=FakePg(),
        host="co-watcher",
        now=lambda: NOW,
    )
    kwargs.update(overrides)
    return backup.run_backup(**kwargs)


# --- what a dump is ---


class TestPure:
    def test_as_user_drops_privileges_with_setpriv(self) -> None:
        """``setpriv``, not ``runuser``: runuser goes through PAM, and PAM cannot
        open a session under the unit's ProtectSystem=strict (verified on the VM)."""
        assert backup.as_user(["pg_dump", "watcher"], "postgres") == [
            "setpriv",
            "--reuid=postgres",
            "--regid=postgres",
            "--init-groups",
            "--reset-env",
            "--",
            "pg_dump",
            "watcher",
        ]

    def test_the_dropped_to_user_inherits_none_of_the_units_environment(self) -> None:
        """Without ``--reset-env`` the ``postgres`` child held the unit's whole
        environment — the check-in key included — readable by any process of
        that uid through ``/proc/<pid>/environ``. A key that forges ``ok`` is
        the one thing the dead-man switch cannot survive."""
        argv = backup.as_user(["pg_dump"], "postgres")
        assert "--reset-env" in argv[: argv.index("--")]

    def test_as_user_without_a_user_is_the_command_itself(self) -> None:
        assert backup.as_user(["pg_dump", "x"], None) == ["pg_dump", "x"]

    def test_parse_toc(self) -> None:
        toc = backup.parse_toc(TOC)
        assert toc.server_version == "16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)"
        assert toc.pg_dump_version == "16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)"
        assert toc.entries == 312
        assert {"public.alembic_version", "public.watched_items"} <= toc.tables_with_data

    def test_object_key_is_the_dump_time(self) -> None:
        """A listing reads as a timeline and the newest name is the newest data."""
        assert backup.object_key("co-watcher", NOW) == "co-watcher/20260912T031702Z.dump"


# --- what the job refuses ---


class TestVerify:
    def test_a_dump_pg_restore_cannot_read_is_refused(self, tmp_path) -> None:
        path = tmp_path / "x.dump"
        path.write_bytes(b"not a dump")
        with pytest.raises(BackupError, match="pg_restore"):
            backup.verify_dump(path, runner=FakePg(fail="pg_restore"))

    def test_a_dump_without_watchers_tables_is_refused(self, tmp_path) -> None:
        """A readable archive of the wrong database is not a backup of this one."""
        path = tmp_path / "x.dump"
        path.write_bytes(DUMP_BYTES)
        empty = TOC.split("6; 2615")[0]
        with pytest.raises(BackupError, match="watched_items"):
            backup.verify_dump(path, runner=FakePg(toc=empty))


class TestTakeDump:
    def test_describes_the_dump(self, tmp_path) -> None:
        pg = FakePg()
        dump = backup.take_dump("watcher", tmp_path, run_as="postgres", runner=pg, now=lambda: NOW)
        assert dump.path.read_bytes() == DUMP_BYTES
        assert dump.size_bytes == len(DUMP_BYTES)
        assert len(dump.sha256) == 64
        assert dump.dumped_at == NOW.replace(microsecond=0)
        assert dump.alembic_head == "2f8bb8f7100a"
        assert dump.toc.entries == 312

    def test_the_database_is_read_as_postgres_and_holds_no_credential(self, tmp_path) -> None:
        """Peer auth over the socket, dropping to ``postgres`` for the two
        commands that talk to the server. pg_restore --list reads only the file,
        so it runs as the job's own user."""
        pg = FakePg()
        backup.take_dump("watcher", tmp_path, run_as="postgres", runner=pg, now=lambda: NOW)
        by_program = {
            (call[call.index("--") + 1] if call[0] == "setpriv" else call[0]): call
            for call in pg.calls
        }
        assert by_program["pg_dump"][0] == "setpriv"
        assert by_program["psql"][0] == "setpriv"
        assert by_program["pg_restore"][0] == "pg_restore"
        assert "--format=custom" in by_program["pg_dump"]
        assert "--no-password" in by_program["pg_dump"]

    def test_a_failed_pg_dump_is_a_failure(self, tmp_path) -> None:
        with pytest.raises(BackupError, match="pg_dump"):
            backup.take_dump(
                "watcher", tmp_path, run_as=None, runner=FakePg(fail="pg_dump"), now=lambda: NOW
            )


# --- the bucket ---


class TestUpload:
    def test_creates_the_object_with_its_metadata(self, client, bucket, tmp_path) -> None:
        summary = _run(client, tmp_path)
        key = "co-watcher/20260912T031702Z.dump"
        assert summary["outcome"] == "uploaded"
        assert summary["object"] == f"gs://{BUCKET}/{key}"
        assert bucket.objects[key] == DUMP_BYTES
        assert bucket.preconditions == [0]
        meta = bucket.metadata[key]
        assert meta["sha256"] == summary["sha256"]
        assert meta["alembic_head"] == "2f8bb8f7100a"
        assert meta["source_host"] == "co-watcher"
        assert meta["server_version"].startswith("16.13")

    def test_the_same_dump_twice_is_unchanged(self, client, bucket, tmp_path) -> None:
        """A 412 on the create is a success when the object holds these bytes."""
        _run(client, tmp_path)
        assert _run(client, tmp_path)["outcome"] == "unchanged"

    def test_a_different_dump_under_the_same_name_is_a_failure(
        self, client, bucket, tmp_path
    ) -> None:
        """Unlike an RDB, two pg_dumps are never the same bytes, so a name
        collision (two runs in one second) must not read as ``unchanged``."""
        _run(client, tmp_path)
        bucket.metadata["co-watcher/20260912T031702Z.dump"]["sha256"] = "0" * 64
        with pytest.raises(BackupError, match="already exists"):
            _run(client, tmp_path)

    def test_a_missing_bucket_fails_before_anything_is_written(self, tmp_path) -> None:
        """Before anything is dumped, too: the bucket is the cheap thing to
        check, and a misspelled one should not cost a pg_dump of production."""
        pg = FakePg()
        with pytest.raises(BackupError, match="not found"):
            _run(FakeClient(FakeBucket(BUCKET), missing=True), tmp_path, runner=pg)
        assert pg.calls == []


# --- the entry point ---


class TestMain:
    @pytest.fixture
    def wired(self, monkeypatch, client):
        checkins: list[tuple[str, dict]] = []
        monkeypatch.setattr(backup.storage, "Client", lambda: client)
        monkeypatch.setattr(backup.subprocess, "run", FakePg())
        monkeypatch.setattr(
            backup,
            "post_checkin",
            lambda status, variables, **_: checkins.append((status, variables)),
        )
        monkeypatch.setenv(backup.BUCKET_ENV, BUCKET)
        return checkins

    def test_success_exits_zero_and_checks_in_ok(self, wired) -> None:
        assert backup.main(["--database", "watcher", "--run-as", "postgres"]) == 0
        ((status, variables),) = wired
        assert status == "ok"
        assert variables["outcome"] == "uploaded"

    def test_failure_exits_non_zero_and_checks_in_alert(self, wired, monkeypatch) -> None:
        monkeypatch.setattr(backup.subprocess, "run", FakePg(fail="pg_dump"))
        assert backup.main(["--database", "watcher"]) == 1
        ((status, variables),) = wired
        assert status == "alert"
        assert "pg_dump" in variables["error"]

    def test_a_check_in_that_cannot_be_sent_never_fails_a_shipped_backup(
        self, monkeypatch, client, bucket
    ) -> None:
        """The dump is in the bucket; the monitoring path must not turn that
        into a failed unit (src.ops.checkin's contract). Driven through the
        real ``post_checkin`` with a base URL ``urlsplit`` refuses."""
        monkeypatch.setattr(backup.storage, "Client", lambda: client)
        monkeypatch.setattr(backup.subprocess, "run", FakePg())
        monkeypatch.setenv(backup.BUCKET_ENV, BUCKET)
        monkeypatch.setenv(checkin.BASE_URL_ENV, "http://[notifier.invalid:9000")
        monkeypatch.setenv(checkin.MONITOR_ID_ENV, "01M24A8CA2GT0M7WE57NEMD0EW")
        monkeypatch.setenv(checkin.API_KEY_ENV, "nk_backup")
        assert backup.main(["--database", "watcher"]) == 0
        assert len(bucket.objects) == 1

    def test_no_bucket_is_a_failure_and_says_so(self, wired, monkeypatch) -> None:
        """No default bucket: guessing one is how bytes land where nobody reads."""
        monkeypatch.delenv(backup.BUCKET_ENV)
        assert backup.main([]) == 2
        ((status, variables),) = wired
        assert status == "alert"
        assert backup.BUCKET_ENV in variables["error"]
