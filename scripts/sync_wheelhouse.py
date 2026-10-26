"""Mirror the private cannobserv package index into the local wheelhouse.

Downloads every object under ``gs://co-gcs-pypi/wheels/`` into ``./.wheelhouse``
(repo root), skipping any file already present with a matching size - except a
file ``uv.lock`` names, which ``uv`` installs: that one is held to the sha256
``pyproject.toml``'s ``[tool.watcher.wheelhouse-sha256]`` pins (#393), fetched
again when the copy here misses it, and refused when the download misses it
too. A size, or GCS's own checksum, comes over the same TLS as the bytes, so
neither tells a spoofed bucket from the real one. ``uv`` then resolves
``co-core`` / ``co-core-aio`` from that directory via the ``[tool.uv]
find-links`` entry in ``pyproject.toml`` (#220). The pin check is
CannObserv/broker#125's, ported.

Runs standalone, *before* ``uv sync`` — it must not import the project (whose
deps are what the wheelhouse provides), so invoke it in an isolated env:

    uv run --no-project --with 'google-cloud-storage>=2,<4' python scripts/sync_wheelhouse.py

Authentication is Application Default Credentials. On the VM/deploy that is the
service-account key at ``GOOGLE_APPLICATION_CREDENTIALS`` (set in
``/etc/watcher/.env``); in CI it is the ADC file written by
``google-github-actions/auth`` (keyless Workload Identity Federation). Either
way the identity needs only ``roles/storage.objectViewer`` on the bucket.

Exit codes: ``0`` success (including a no-op re-run) · ``1`` failure (auth,
network, a missing bucket, a locked file with no pin or one the bucket does not
list, an object named outside the wheelhouse, or a download that is not the
pinned file; every refusal is named, and the files after one still sync). CI
runs it into an empty wheelhouse in every job, so a download that misses its
pin fails CI. The systemd unit runs it as a non-fatal ``ExecStartPre`` (``-``
prefix): a transient failure is surfaced to the journal and the start goes on,
to ``scripts/check_wheelhouse.py``, the fail-closed offline step after it, which
stops the start if a locked file is missing or misses its pin.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# A sibling, not a package import: this runs as `python scripts/sync_wheelhouse.py`,
# so scripts/ is first on the path. The pin logic lives there because the unit's
# offline check must import only the standard library.
from check_wheelhouse import TABLE, pins, sha256
from google.cloud import storage

BUCKET = os.environ.get("WATCHER_WHEELHOUSE_BUCKET", "co-gcs-pypi")
PREFIX = os.environ.get("WATCHER_WHEELHOUSE_PREFIX", "wheels/")
PROJECT = Path(__file__).resolve().parent.parent


def sync() -> int:
    """Mirror ``gs://{BUCKET}/{PREFIX}`` into the checkout's wheelhouse; return an exit code."""
    dest = PROJECT / ".wheelhouse"
    dest.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = 0
    try:
        pinned = pins(PROJECT)
        unlisted = set(pinned)
        # Every refusal is collected and the loop goes on, so one refused file
        # does not leave the files after it unsynced; the run fails at the end.
        refused = []
        root = dest.resolve()
        client = storage.Client()
        for blob in client.list_blobs(BUCKET, prefix=PREFIX):
            name = blob.name.removeprefix(PREFIX)
            if not name:  # the prefix "directory" placeholder object, if any
                continue
            target = dest / name
            # The bucket names the path it writes: one outside the wheelhouse
            # (`../pyproject.toml`, an absolute name) is held to no pin and could
            # rewrite the table the unit's check reads.
            if root not in target.resolve().parents:
                refused.append(f"{name} names a path outside {dest}")
                continue
            digest = pinned.get(name)
            unlisted.discard(name)
            # A locked file is skipped only when it is the pinned one. Any other
            # is skipped when a same-size file is present: published artifacts
            # are server-side immutable (cannobserv#215), nothing installs it,
            # and it meets its pin when a lock names it.
            if target.exists() and (
                sha256(target) == digest
                if digest is not None
                else target.stat().st_size == blob.size
            ):
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Download to a sibling temp file then atomically rename, so an
            # interrupted run never leaves a partial wheel in place (which a
            # concurrent reader, or a same-size coincidence, could mistake for
            # a complete one). os.replace is atomic within the same directory.
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
            os.close(fd)
            try:
                blob.download_to_filename(tmp)
                # Refused before it replaces anything: a planted copy stays as
                # found, for whoever reads why, and the unit's check refuses it.
                if digest is not None and (got := sha256(tmp)) != digest:
                    refused.append(f"{name} downloads with sha256 {got}, not the pinned {digest}")
                    continue
                os.replace(tmp, target)
            finally:
                Path(tmp).unlink(missing_ok=True)
            downloaded += 1
        # A locked file the loop never reached was held to nothing: a copy here
        # under that name would pass to uv unchecked.
        if unlisted:
            refused.append(
                f"uv.lock names {', '.join(sorted(unlisted))}, which the bucket does not list"
            )
        if refused:
            raise ValueError(f"{'; '.join(refused)} (pyproject.toml's [tool.watcher.{TABLE}])")
    except Exception as exc:  # broad by design: auth/network/bucket failures degrade identically
        print(f"error: could not sync gs://{BUCKET}/{PREFIX}: {exc}", file=sys.stderr)
        return 1

    print(f"wheelhouse in sync: {downloaded} downloaded, {skipped} already present -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(sync())
