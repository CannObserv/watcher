"""scripts/sync_wheelhouse.py and the digests it holds a locked file to (#393).

Ported from CannObserv/broker#125. The sync used to skip any file whose size
matched its object, so a same-size file with other bytes stayed forever. A file
``uv.lock`` names is now skipped only when it matches its pin in
``[tool.watcher.wheelhouse-sha256]``; otherwise it is fetched again, and a
download that misses the pin is refused, not put in place. A size, or GCS's own
checksum, comes over the same TLS as the bytes, so neither tells a spoofed
bucket from the real one.

The unit runs this fail-open and ``scripts/check_wheelhouse.py`` after it
fail-closed (tests/deploy/test_wheelhouse_unit.py); CI runs it into an empty
wheelhouse in every job, so a download that misses its pin fails CI.
"""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

from tests.ops.gcs_fakes import FakeBlob, FakeBucket, FakeClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
TABLE = "wheelhouse-sha256"


def _load_sync():
    """Import the script as ``python scripts/sync_wheelhouse.py`` runs it: with
    ``scripts/`` first on the path, where its ``check_wheelhouse`` import resolves."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "sync_wheelhouse", SCRIPTS / "sync_wheelhouse.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


sync_wheelhouse = _load_sync()

WHEEL = "co_core-9.9.0-py3-none-any.whl"
SDIST = "co_core-9.9.0.tar.gz"
OTHER = "other-1.0-py3-none-any.whl"
GOOD = {WHEEL: b"the wheel", SDIST: b"the sdist", OTHER: b"other"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


LOCK = f"""version = 1

[[package]]
name = "co-core"
version = "9.9.0"
source = {{ registry = ".wheelhouse" }}
sdist = {{ path = "{SDIST}" }}
wheels = [
    {{ path = "{WHEEL}" }},
]
"""


def pyproject(digests: dict[str, str]) -> str:
    rows = "".join(f'"{name}" = "{value}"\n' for name, value in digests.items())
    return f'[project]\nname = "t"\n\n[tool.watcher.{TABLE}]\n{rows}'


class Host:
    """A checkout's root, its wheelhouse, and the bucket the sync mirrors."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.project = tmp
        self.wheelhouse = tmp / ".wheelhouse"
        self.bucket = FakeBucket("co-gcs-pypi")
        for name, data in GOOD.items():
            self.bucket.objects[f"wheels/{name}"] = data
        self.downloads: list[str] = []
        (tmp / "uv.lock").write_text(LOCK)
        self.pin({WHEEL: sha(GOOD[WHEEL]), SDIST: sha(GOOD[SDIST])})
        client = FakeClient(self.bucket)
        real = FakeBlob.download_to_filename
        host = self

        def download(blob, filename, timeout=None):
            host.downloads.append(blob.name.removeprefix("wheels/"))
            real(blob, filename, timeout=timeout)

        monkeypatch.setattr(FakeBlob, "download_to_filename", download)
        monkeypatch.setattr(sync_wheelhouse.storage, "Client", lambda: client)
        monkeypatch.setattr(sync_wheelhouse, "PROJECT", tmp)

    def pin(self, digests: dict[str, str]) -> None:
        (self.project / "pyproject.toml").write_text(pyproject(digests))

    def plant(self, name: str, data: bytes) -> None:
        self.wheelhouse.mkdir(exist_ok=True)
        (self.wheelhouse / name).write_bytes(data)

    def sync(self, capsys) -> tuple[int, str]:
        rc = sync_wheelhouse.sync()
        return rc, capsys.readouterr().err


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(tmp_path, monkeypatch)


class TestALockedFile:
    def test_an_empty_wheelhouse_is_filled_with_the_pinned_files(self, host, capsys):
        """CI's path in every job: nothing present, everything fetched and held."""
        rc, err = host.sync(capsys)
        assert rc == 0, err
        for name, data in GOOD.items():
            assert (host.wheelhouse / name).read_bytes() == data

    def test_a_same_size_wheel_with_other_bytes_is_fetched_again(self, host, capsys):
        """The swap a size-only skip kept forever: same name, same size."""
        planted = b"the WHEEL"
        assert len(planted) == len(GOOD[WHEEL])
        host.plant(WHEEL, planted)
        rc, err = host.sync(capsys)
        assert rc == 0, err
        assert (host.wheelhouse / WHEEL).read_bytes() == GOOD[WHEEL]
        assert WHEEL in host.downloads

    def test_a_download_that_is_not_the_pinned_file_is_refused(self, host, capsys):
        """What a spoofed channel serves: its own wheel, and a checksum to match."""
        host.bucket.objects[f"wheels/{WHEEL}"] = b"the WHEEL"
        rc, err = host.sync(capsys)
        assert rc == 1
        assert WHEEL in err
        assert sha(b"the WHEEL") in err and sha(GOOD[WHEEL]) in err
        assert not (host.wheelhouse / WHEEL).exists()
        assert not list(host.wheelhouse.glob(".*.part"))

    def test_a_planted_file_is_left_as_found_when_the_refetch_misses_too(self, host, capsys):
        """Evidence for whoever reads why; the unit's check refuses it either way."""
        host.plant(WHEEL, b"the wheeL")
        host.bucket.objects[f"wheels/{WHEEL}"] = b"the WHEEL"
        rc, err = host.sync(capsys)
        assert rc == 1
        assert WHEEL in err
        assert (host.wheelhouse / WHEEL).read_bytes() == b"the wheeL"

    def test_a_locked_file_the_bucket_does_not_list_fails_the_sync(self, host, capsys):
        """The loop visits what the listing names, so a copy planted under a name
        the bucket lacks would otherwise be held to nothing."""
        del host.bucket.objects[f"wheels/{WHEEL}"]
        host.plant(WHEEL, b"the WHEEL")
        rc, err = host.sync(capsys)
        assert rc == 1
        assert WHEEL in err and "does not list" in err

    def test_a_file_that_matches_its_pin_is_not_fetched_again(self, host, capsys):
        host.plant(WHEEL, GOOD[WHEEL])
        host.plant(SDIST, GOOD[SDIST])
        rc, err = host.sync(capsys)
        assert rc == 0, err
        assert WHEEL not in host.downloads and SDIST not in host.downloads

    def test_the_sdist_is_held_to_its_pin_as_well(self, host, capsys):
        host.plant(SDIST, b"the SDIST")
        rc, err = host.sync(capsys)
        assert rc == 0, err
        assert (host.wheelhouse / SDIST).read_bytes() == GOOD[SDIST]

    def test_a_locked_file_with_no_digest_fails_the_sync(self, host, capsys):
        host.pin({WHEEL: sha(GOOD[WHEEL])})
        rc, err = host.sync(capsys)
        assert rc == 1
        assert SDIST in err and TABLE in err
        assert "sha256sum .wheelhouse/" in err
        assert not host.downloads

    @pytest.mark.parametrize("value", ["", "abc123", "A" * 64])
    def test_a_pin_that_is_not_a_sha256_fails_the_sync(self, host, capsys, value):
        """A blank pin was falsy in broker's first cut, so the file fell back to the size skip."""
        host.pin({WHEEL: value, SDIST: sha(GOOD[SDIST])})
        host.plant(WHEEL, b"the WHEEL")
        rc, err = host.sync(capsys)
        assert rc == 1
        assert WHEEL in err and "not a sha256" in err
        assert not host.downloads


class TestAFileTheLockDoesNotName:
    def test_a_same_size_file_is_still_skipped(self, host, capsys):
        """Nothing installs it: the bucket holds every cannobserv release, and
        only the locked ones reach the venv. It meets its pin when a lock names it."""
        host.plant(OTHER, b"OTHER")
        rc, err = host.sync(capsys)
        assert rc == 0, err
        assert OTHER not in host.downloads
        assert (host.wheelhouse / OTHER).read_bytes() == b"OTHER"
