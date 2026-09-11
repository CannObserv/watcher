"""Ship Watcher's database to a bucket, nightly (#296 D8).

Production had never been backed up — no timer, no dump directory — until this.
The job follows CannObserv/broker#4 wherever Postgres allows, and is built to be
dumb in the ways that keep a backup honest:

- **It holds no database credential.** The unit runs as root under a sandbox
  (so it can read its 0400 key), and the two commands that talk to the server —
  ``pg_dump`` and a ``psql`` for the schema version — drop to the ``postgres``
  OS user and connect over the local socket with peer auth. ``setpriv``, not
  ``runuser``: runuser goes through PAM, and PAM cannot open a session under
  ``ProtectSystem=strict`` (tried on the VM). Tests pass a DSN instead.
- **It verifies before it ships.** ``pg_restore --list`` must read the archive
  and find the data sections of ``public.alembic_version`` and
  ``public.watched_items``: a readable dump of the wrong database is refused,
  never uploaded under a name that says backup.
- **The object is named by the dump's start time** — when ``pg_dump`` took its
  snapshot — so a listing is a timeline and the newest name is the newest data.
- **It creates, and never overwrites or deletes.** ``if_generation_match=0`` in
  code; ``objectCreator`` + ``objectViewer`` and no ``delete`` at IAM; retention
  is the bucket's lifecycle rule, so a compromised host cannot erase its own
  history. Unlike broker's RDB, two dumps are never the same bytes, so a 412 is
  ``unchanged`` only when the object's recorded sha256 matches — otherwise it is
  a name collision, and a failure.
- **Failure is loud, and so is silence.** A failed run is a failed unit and an
  ``alert`` check-in; a successful one checks in ``ok``. The monitor alarms when
  neither arrives (``src.ops.checkin``, D9).

Restore is ``src.ops.restore``; docs/RECOVERY.md is the runbook around both.
"""

import argparse
import hashlib
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

from src.core.logging import configure_logging, get_logger
from src.ops.checkin import post_checkin

# Literal rather than __name__: the timer runs this module via ``python -m``.
logger = get_logger("src.ops.backup")

BUCKET_ENV = "WATCHER_BACKUP_BUCKET"
PREFIX_ENV = "WATCHER_BACKUP_PREFIX"
OBJECT_SUFFIX = ".dump"
# Basic-format ISO 8601, UTC. Sorts as it reads, and no ':' for a shell to trip on.
KEY_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
CONTENT_TYPE = "application/octet-stream"

#: A dump that lacks either data section is not a backup of this database.
REQUIRED_TABLES = ("public.alembic_version", "public.watched_items")

# Bounds on the slow calls. The database is ~20 MB after retention (#296 D7);
# these are generous for that and short enough that a wedged call is a failed
# unit rather than a hang.
PG_DUMP_TIMEOUT_SECONDS = 1800
QUERY_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 600.0
LIST_TIMEOUT_SECONDS = 30.0

_HEADER_RE = re.compile(r"^;\s+(Dumped from database version|Dumped by pg_dump version): (.+)$")
_ENTRIES_RE = re.compile(r"^;\s+TOC Entries: (\d+)$")
_TABLE_DATA_RE = re.compile(r"^\d+; \d+ \d+ TABLE DATA (\S+) (\S+) ")

Runner = Callable[..., subprocess.CompletedProcess]


class BackupError(Exception):
    """Anything that means the dump was not shipped."""


@dataclass(frozen=True)
class Toc:
    """What ``pg_restore --list`` says about an archive."""

    server_version: str | None = None
    pg_dump_version: str | None = None
    entries: int | None = None
    tables_with_data: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Dump:
    """A verified dump on local disk, and what it says about itself."""

    path: Path
    size_bytes: int
    sha256: str
    dumped_at: datetime
    alembic_head: str | None
    toc: Toc


# --- pure ---


def as_user(argv: list[str], run_as: str | None) -> list[str]:
    """Prefix ``argv`` to run as ``run_as`` via ``setpriv``; unchanged when None."""
    if run_as is None:
        return argv
    return ["setpriv", f"--reuid={run_as}", f"--regid={run_as}", "--init-groups", "--", *argv]


def parse_toc(text: str) -> Toc:
    """The header fields and the tables with a data section."""
    versions: dict[str, str] = {}
    entries: int | None = None
    tables: set[str] = set()
    for line in text.splitlines():
        if match := _HEADER_RE.match(line):
            versions[match.group(1)] = match.group(2).strip()
        elif match := _ENTRIES_RE.match(line):
            entries = int(match.group(1))
        elif match := _TABLE_DATA_RE.match(line):
            tables.add(f"{match.group(1)}.{match.group(2)}")
    return Toc(
        server_version=versions.get("Dumped from database version"),
        pg_dump_version=versions.get("Dumped by pg_dump version"),
        entries=entries,
        tables_with_data=frozenset(tables),
    )


def object_key(prefix: str, dumped_at: datetime) -> str:
    stamp = dumped_at.astimezone(UTC).strftime(KEY_TIME_FORMAT)
    return f"{prefix.strip('/')}/{stamp}{OBJECT_SUFFIX}"


def iso(at: datetime) -> str:
    """ISO 8601, UTC, second precision, ``Z``."""
    return at.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return " | ".join((text or "").strip().splitlines()[-3:])


# --- effects on the database and the file ---


def read_alembic_head(database: str, *, run_as: str | None, runner: Runner) -> str | None:
    """The schema version, recorded so a restore can be checked against it."""
    argv = as_user(
        [
            "psql",
            "--no-password",
            "--no-psqlrc",
            "--quiet",
            "--tuples-only",
            "--no-align",
            f"--dbname={database}",
            "--command=SELECT version_num FROM alembic_version",
        ],
        run_as,
    )
    result = runner(
        argv, capture_output=True, text=True, check=False, timeout=QUERY_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        raise BackupError(f"psql could not read alembic_version: {_tail(result.stderr)}")
    return result.stdout.strip() or None


def run_pg_dump(database: str, out: Path, *, run_as: str | None, runner: Runner) -> None:
    """Custom format to ``out``. Written through an fd this process opened, so
    ``postgres`` writes a file it could not otherwise create or read."""
    argv = as_user(["pg_dump", "--format=custom", "--no-password", f"--dbname={database}"], run_as)
    with out.open("wb") as handle:
        result = runner(
            argv,
            stdout=handle,
            stderr=subprocess.PIPE,
            check=False,
            timeout=PG_DUMP_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        raise BackupError(f"pg_dump exited {result.returncode}: {_tail(result.stderr)}")


def verify_dump(path: Path, *, runner: Runner) -> Toc:
    """Refuse an archive ``pg_restore`` cannot read, or one of the wrong database."""
    result = runner(
        ["pg_restore", "--list", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise BackupError(f"pg_restore --list rejected {path.name}: {_tail(result.stderr)}")
    toc = parse_toc(result.stdout)
    missing = [table for table in REQUIRED_TABLES if table not in toc.tables_with_data]
    if missing:
        raise BackupError(f"dump has no data for {', '.join(missing)}; refusing to ship it")
    return toc


def take_dump(
    database: str,
    workdir: Path,
    *,
    run_as: str | None,
    runner: Runner,
    now: Callable[[], datetime],
) -> Dump:
    """Dump, verify, describe. The start time names it: ``pg_dump`` takes its
    snapshot as it begins."""
    dumped_at = now().astimezone(UTC).replace(microsecond=0)
    alembic_head = read_alembic_head(database, run_as=run_as, runner=runner)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "watcher.dump"
    run_pg_dump(database, path, run_as=run_as, runner=runner)
    toc = verify_dump(path, runner=runner)
    return Dump(
        path=path,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        dumped_at=dumped_at,
        alembic_head=alembic_head,
        toc=toc,
    )


# --- effects on the bucket ---


def preflight(client: storage.Client, bucket: str, prefix: str) -> None:
    """Prove the bucket is there and listable by this identity.

    A one-object listing, not ``exists()``: the SDK swallows a missing bucket's
    404 and returns ``False``, which is exactly the misconfiguration this check
    exists to catch (replicator#7 CR #1). The listing is lazy, so it is advanced.
    """
    listing = client.list_blobs(
        bucket, max_results=1, prefix=f"{prefix.strip('/')}/", timeout=LIST_TIMEOUT_SECONDS
    )
    try:
        next(iter(listing), None)
    except NotFound as exc:
        raise BackupError(f"bucket {bucket!r} not found, or not listable: {exc}") from exc


def upload(client: storage.Client, bucket: str, key: str, dump: Dump, *, host: str) -> str:
    """Create the object; ``unchanged`` only if this very dump is already there."""
    blob = client.bucket(bucket).blob(key)
    blob.metadata = {
        "dumped_at": iso(dump.dumped_at),
        "sha256": dump.sha256,
        "size_bytes": str(dump.size_bytes),
        "alembic_head": dump.alembic_head or "",
        "server_version": dump.toc.server_version or "",
        "pg_dump_version": dump.toc.pg_dump_version or "",
        "toc_entries": "" if dump.toc.entries is None else str(dump.toc.entries),
        "source_host": host,
    }
    try:
        blob.upload_from_filename(
            str(dump.path),
            content_type=CONTENT_TYPE,
            if_generation_match=0,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
    except PreconditionFailed:
        existing = client.bucket(bucket).get_blob(key, timeout=LIST_TIMEOUT_SECONDS)
        if existing is not None and (existing.metadata or {}).get("sha256") == dump.sha256:
            return "unchanged"
        raise BackupError(f"{key} already exists with different contents") from None
    return "uploaded"


# --- orchestration ---


def run_backup(
    *,
    database: str,
    bucket: str,
    prefix: str,
    client: storage.Client,
    workdir: Path,
    run_as: str | None,
    runner: Runner | None = None,
    host: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    """One run: dump, verify, preflight, create. Returns the summary it logged.

    ``runner`` defaults to ``subprocess.run`` looked up at call time, not bound
    as a default — so a test that replaces it can never fall through to the
    real binaries.

    Raises ``BackupError`` for any failure, whatever its type — a revoked key
    surfaces from google.auth as a ``RefreshError``, a transport fault as a
    ``TransportError``, neither a ``GoogleAPICallError`` (broker#4's code review).
    """
    host = host or socket.gethostname()
    runner = runner if runner is not None else subprocess.run
    try:
        dump = take_dump(database, workdir, run_as=run_as, runner=runner, now=now)
        key = object_key(prefix, dump.dumped_at)
        preflight(client, bucket, prefix)
        outcome = upload(client, bucket, key, dump, host=host)
    except Exception as exc:
        error = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
        logger.error("Backup failed: %s", error, extra={"bucket": bucket})
        raise BackupError(error) from exc
    summary = {
        "outcome": outcome,
        "object": f"gs://{bucket}/{key}",
        "dumped_at": iso(dump.dumped_at),
        "size_bytes": dump.size_bytes,
        "sha256": dump.sha256,
        "alembic_head": dump.alembic_head,
        "source_host": host,
    }
    logger.info("Backup %s: %s", outcome, summary["object"], extra=summary)
    return summary


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] = os.environ) -> int:
    """Timer entrypoint. Exit 0 only when the dump is in the bucket."""
    parser = argparse.ArgumentParser(description="Watcher database dump to GCS")
    parser.add_argument("--database", default="watcher", help="name, or a DSN in tests")
    parser.add_argument("--run-as", default=None, help="OS user for pg_dump/psql (peer auth)")
    args = parser.parse_args(argv)

    configure_logging()
    host = socket.gethostname()

    def fail(code: int, error: str) -> int:
        post_checkin(
            "alert", {"source": host, "outcome": "failed", "error": error}, environ=environ
        )
        return code

    bucket = environ.get(BUCKET_ENV)
    if not bucket:
        # No default bucket: guessing one is how bytes land where nobody reads.
        logger.error("%s not set — nowhere to ship the dump", BUCKET_ENV)
        return fail(2, f"{BUCKET_ENV} not set")
    prefix = environ.get(PREFIX_ENV) or host

    with tempfile.TemporaryDirectory(prefix="watcher-backup-") as work:
        try:
            # Built first, so a missing or unreadable key fails before the dump.
            client = storage.Client()
        except Exception as exc:  # google.auth raises its own hierarchy
            error = f"{type(exc).__name__}: {exc}"
            logger.error("Backup failed before it could start: %s", error)
            return fail(1, error)
        try:
            summary = run_backup(
                database=args.database,
                bucket=bucket,
                prefix=prefix,
                client=client,
                workdir=Path(work),
                run_as=args.run_as,
                host=host,
            )
        except BackupError as exc:
            return fail(1, str(exc))
    post_checkin("ok", summary, environ=environ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
