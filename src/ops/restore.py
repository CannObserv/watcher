"""Bring a shipped dump back (#296 D8).

Three steps, each refusing rather than guessing: **find** a dump (by key, or the
newest name under the prefix — names are timestamps); **fetch** it and prove it
is what the backup recorded (sha256 against the object's metadata, then the same
``pg_restore --list`` check the backup ran); **restore** it into an existing,
empty database, in one transaction, so a failure leaves nothing half-loaded.

The restore reads the archive on stdin, so ``postgres`` never needs to read a
file root wrote, and it preserves owners and grants: the dump carries the
two-role model's table ACLs and default privileges (#259). What it cannot carry
is the database-level ``GRANT CONNECT`` — ``pg_dump`` without ``--create`` has
no database to put it on — so the runbook re-runs ``scripts/setup-db-roles.sql``
after every restore. docs/RECOVERY.md is that runbook.

This is also the migration's transfer path (#296 D10): the cutover dump moves
through the bucket, so the procedure an incident would need is the one the move
exercises on real data.

**The source host is always named.** A restore runs on a different host from
the one that shipped the dump — the cutover's co-watcher, an incident's
replacement — so the backup's default prefix (this hostname) is here the one
prefix never wanted, and once the new host's own timer has run it would find a
real, verifiable dump of the wrong database. ``--latest`` takes ``--prefix``;
``--list`` without one shows every host's dumps.

    python -m src.ops.restore --list
    python -m src.ops.restore --latest --prefix watcher --download-only /root/watcher-restore
    python -m src.ops.restore --latest --prefix watcher --into watcher --run-as postgres
"""

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import storage

from src.core.logging import configure_logging, get_logger
from src.ops.backup import (
    BUCKET_ENV,
    LIST_TIMEOUT_SECONDS,
    OBJECT_SUFFIX,
    PG_DUMP_TIMEOUT_SECONDS,
    UPLOAD_TIMEOUT_SECONDS,
    BackupError,
    Runner,
    as_user,
    sha256_file,
    verify_dump,
)

logger = get_logger("src.ops.restore")

# What `--list` prints beside each name, in this order.
_LISTED_METADATA = ("dumped_at", "alembic_head", "size_bytes", "source_host", "sha256")


class RestoreError(Exception):
    """Anything that means nothing was restored."""


def _under(prefix: str | None) -> str:
    """The listing prefix for a host's dumps; "" for the whole bucket."""
    return f"{prefix.strip('/')}/" if prefix else ""


def list_snapshots(
    client: storage.Client, bucket: str, prefix: str | None
) -> list[tuple[str, dict]]:
    """Every dump under ``prefix`` (every host's, if None), in name order, with
    the metadata the backup wrote. Within one host, name order is time order."""
    blobs = client.list_blobs(bucket, prefix=_under(prefix) or None, timeout=LIST_TIMEOUT_SECONDS)
    return sorted(
        (blob.name, dict(blob.metadata or {}))
        for blob in blobs
        if blob.name.endswith(OBJECT_SUFFIX)
    )


def latest_key(client: storage.Client, bucket: str, prefix: str) -> str:
    """The newest dump the named host shipped."""
    snapshots = list_snapshots(client, bucket, prefix)
    if not snapshots:
        raise RestoreError(f"no dumps under gs://{bucket}/{_under(prefix)}")
    return snapshots[-1][0]


def _private_dir(dest_dir: Path) -> None:
    """Create ``dest_dir`` 0700, or accept an existing one only if it is this
    user's own, a real directory, and closed to everyone else.

    The fetch runs as root and writes the whole production database: a
    directory another user made first (``/tmp/restore``) would let them read
    it, or plant a link for root to write through.
    """
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = dest_dir.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise RestoreError(f"{dest_dir} is not a directory; refusing it")
    if st.st_uid != os.geteuid():
        raise RestoreError(f"{dest_dir} is owned by uid {st.st_uid}, not this user; refusing it")
    if st.st_mode & 0o077:
        raise RestoreError(
            f"{dest_dir} is not private (mode {stat.S_IMODE(st.st_mode):o}); "
            "use a new directory, or chmod 700 it"
        )


def fetch(client: storage.Client, bucket: str, key: str, dest_dir: Path, *, runner: Runner) -> Path:
    """Download ``key`` into ``dest_dir`` and prove it is the dump that was shipped.

    The file is created ``O_EXCL | O_NOFOLLOW`` at 0600 in a private directory,
    so nothing already at the name — a stale dump, a planted link — is written
    through, and no other user can read the result. A failed fetch removes it.
    """
    blob = client.bucket(bucket).get_blob(key, timeout=LIST_TIMEOUT_SECONDS)
    if blob is None:
        raise RestoreError(f"gs://{bucket}/{key} not found")
    expected = (blob.metadata or {}).get("sha256")
    if not expected:
        raise RestoreError(f"gs://{bucket}/{key} carries no recorded sha256; refusing it")
    _private_dir(dest_dir)
    path = dest_dir / Path(key).name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise RestoreError(f"{path} already exists; refusing to write over it") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            try:
                blob.download_to_file(handle, timeout=UPLOAD_TIMEOUT_SECONDS)
            except NotFound as exc:
                raise RestoreError(f"gs://{bucket}/{key} not found") from exc
        actual = sha256_file(path)
        if actual != expected:
            raise RestoreError(f"{key}: sha256 {actual} does not match the recorded {expected}")
        try:
            verify_dump(path, runner=runner)
        except BackupError as exc:
            raise RestoreError(str(exc)) from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def restore_into(path: Path, database: str, *, run_as: str | None, runner: Runner) -> None:
    """Load ``path`` into an existing, empty ``database``, all or nothing."""
    argv = as_user(
        [
            "pg_restore",
            "--no-password",
            "--exit-on-error",
            "--single-transaction",
            f"--dbname={database}",
        ],
        run_as,
    )
    with path.open("rb") as handle:
        result = runner(
            argv,
            stdin=handle,
            capture_output=True,
            text=True,
            check=False,
            timeout=PG_DUMP_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        tail = " | ".join((result.stderr or "").strip().splitlines()[-3:])
        raise RestoreError(f"pg_restore exited {result.returncode}: {tail}")


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] = os.environ) -> int:
    """Operator entrypoint; see the module docstring."""
    parser = argparse.ArgumentParser(description="Restore a Watcher database dump from GCS")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--list", action="store_true", help="list dumps and exit")
    which.add_argument("--latest", action="store_true", help="the newest dump")
    which.add_argument("--object", metavar="KEY", help="a dump by its object key")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--into", metavar="DATABASE", help="restore into this empty database")
    where.add_argument("--download-only", metavar="DIR", type=Path, help="fetch and verify only")
    parser.add_argument(
        "--prefix",
        metavar="HOST",
        default=None,
        help="the host whose dumps to use (required with --latest; never this host's by default)",
    )
    parser.add_argument("--run-as", default=None, help="OS user for pg_restore (peer auth)")
    args = parser.parse_args(argv)

    configure_logging()
    bucket = environ.get(BUCKET_ENV)
    if not bucket:
        print(f"{BUCKET_ENV} not set", file=sys.stderr)
        return 2
    if args.latest and not args.prefix:
        print(
            "say whose: --latest needs --prefix HOST, the host that shipped the dump "
            "(--list shows every host's)",
            file=sys.stderr,
        )
        return 2
    client = storage.Client()

    try:
        if args.list:
            snapshots = list_snapshots(client, bucket, args.prefix)
            for name, meta in snapshots:
                print(name, *(f"{field}={meta.get(field, '')}" for field in _LISTED_METADATA))
            if not snapshots:
                print(f"no dumps under gs://{bucket}/{_under(args.prefix)}", file=sys.stderr)
            return 0
        if not (args.into or args.download_only):
            print("say where: --into DATABASE or --download-only DIR", file=sys.stderr)
            return 2
        key = args.object or latest_key(client, bucket, args.prefix)
        if args.download_only:
            path = fetch(client, bucket, key, args.download_only, runner=subprocess.run)
            print(f"verified: {path}")
            return 0
        with tempfile.TemporaryDirectory(prefix="watcher-restore-") as work:
            path = fetch(client, bucket, key, Path(work), runner=subprocess.run)
            restore_into(path, args.into, run_as=args.run_as, runner=subprocess.run)
    except RestoreError as exc:
        logger.error("Restore failed: %s", exc)
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"restored gs://{bucket}/{key} into {args.into}")
    print("next: re-run scripts/setup-db-roles.sql against it, then the gates in docs/RECOVERY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
