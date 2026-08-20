"""Tests for the blob reader (#275).

The two error types carry the whole decision the apply path makes: a scheme this
build cannot read is permanent (a re-issued command's fact would say the same
thing), while a blob that is merely missing may be transient.
"""

import os

import pytest
from google.api_core import exceptions as gcs_exceptions

import src.core.blobs as blobs_mod
from src.core.blobs import (
    GCS_BLOB_CREDENTIALS_ENV,
    BlobUnreadable,
    UnsupportedBlobScheme,
    aread_blob,
    blob_file,
    read_blob,
)


class TestReadBlob:
    def test_reads_a_file_uri(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        assert read_blob(f"file://{blob}") == b"<p>hi</p>"

    def test_missing_file_is_unreadable_not_unsupported(self, tmp_path):
        with pytest.raises(BlobUnreadable):
            read_blob(f"file://{tmp_path}/reaped-away.bin")

    def test_unknown_scheme_is_unsupported(self):
        # A backend no build of this module has ever read.
        with pytest.raises(UnsupportedBlobScheme):
            read_blob("s3://co-temp-blobs/ab/cdef")

    def test_missing_uri_is_unreadable(self):
        # A fact that named no blob: nothing about the backend is known, so this
        # is the re-issuable side — and bounded by the caller's cap either way.
        with pytest.raises(BlobUnreadable):
            read_blob(None)

    def test_the_two_error_types_are_siblings(self):
        # The apply path branches on both; if either subclassed the other,
        # ordering the excepts wrong would silently restore the loop (CR-6).
        assert not issubclass(UnsupportedBlobScheme, BlobUnreadable)
        assert not issubclass(BlobUnreadable, UnsupportedBlobScheme)


class TestBlobFile:
    def test_a_local_blob_is_yielded_in_place_and_survives(self, tmp_path):
        # MUST-7: a file:// blob is already a local file, and it is
        # REPLICATOR'S file. Copying it would double every check's disk I/O;
        # deleting it on exit would destroy the fact's own evidence (CR-5).
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        with blob_file(f"file://{blob}") as path:
            assert path == blob
            assert path.read_bytes() == b"<p>hi</p>"

        assert blob.exists()

    def test_a_spooled_copy_is_removed_on_exit(self, tmp_path, monkeypatch):
        # The contract the future non-local arm relies on: whatever this
        # function creates, it cleans up — including when the body raises.
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")
        spooled: list = []
        monkeypatch.setattr("src.core.blobs.SUPPORTED_SCHEMES", ())
        monkeypatch.setattr(
            "src.core.blobs._SPOOLERS",
            {"file": lambda parsed, fh: fh.write(blob.read_bytes())},
        )

        with pytest.raises(RuntimeError):
            with blob_file(f"file://{blob}") as path:
                spooled.append(path)
                assert path != blob
                assert path.read_bytes() == b"<p>hi</p>"
                raise RuntimeError("the caller blew up mid-read")

        assert spooled and not spooled[0].exists()
        assert blob.exists()

    def test_a_uri_naming_another_host_is_refused(self, tmp_path):
        # CR-9: urlparse drops the netloc and url2pathname returns the bare
        # path, so `file://otherhost/x` used to open the LOCAL /x. On a
        # same-layout host that is a successful read of the wrong bytes, under
        # a real command, with nothing anywhere reporting an error. MUST-7 says
        # host-local; a URI that names another host is not this host's to read.
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        with pytest.raises(BlobUnreadable):
            with blob_file(f"file://otherhost{blob}"):
                pass  # pragma: no cover - never entered

    def test_localhost_and_empty_authority_are_this_host(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        assert read_blob(f"file://{blob}") == b"<p>hi</p>"
        assert read_blob(f"file://localhost{blob}") == b"<p>hi</p>"

    def test_a_spooler_raising_anything_becomes_blob_unreadable(self, tmp_path, monkeypatch):
        # CR-10: a backend arm raises what its SDK raises — a 403 from a GCS
        # client is not an OSError. Untyped, it would escape apply_fetch_blob
        # past both excepts, exhaust the retries, and leave the row IN_FLIGHT
        # for the reaper to resurrect forever: the wedge #275 removed.
        def _boom(parsed, fh):
            raise RuntimeError("403 Forbidden")

        monkeypatch.setattr("src.core.blobs.SUPPORTED_SCHEMES", ())
        monkeypatch.setattr("src.core.blobs._SPOOLERS", {"file": _boom})

        with pytest.raises(BlobUnreadable):
            with blob_file("file:///tmp/whatever.bin"):
                pass  # pragma: no cover - never entered

    def test_a_spooler_may_still_declare_a_permanent_failure(self, tmp_path, monkeypatch):
        # The wrapper must not swallow the arm's own verdict: an IAM refusal is
        # permanent, and re-fetching three times buys nothing.
        def _forbidden(parsed, fh):
            raise UnsupportedBlobScheme("no objectViewer on the temp bucket")

        monkeypatch.setattr("src.core.blobs.SUPPORTED_SCHEMES", ())
        monkeypatch.setattr("src.core.blobs._SPOOLERS", {"file": _forbidden})

        with pytest.raises(UnsupportedBlobScheme):
            with blob_file("file:///tmp/whatever.bin"):
                pass  # pragma: no cover - never entered

    def test_unsupported_scheme_raises_before_any_temp_file(self):
        with pytest.raises(UnsupportedBlobScheme):
            with blob_file("s3://co-temp-blobs/ab/cdef"):
                pass  # pragma: no cover - never entered


class TestAreadBlob:
    async def test_reads_off_the_event_loop(self, tmp_path):
        # CR-2: the apply task runs in the one load-bearing process, next to the
        # API and the fact consumer. A multi-MB blob read inline stalls both.
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        assert await aread_blob(f"file://{blob}") == b"<p>hi</p>"

    async def test_propagates_the_typed_errors(self, tmp_path):
        with pytest.raises(BlobUnreadable):
            await aread_blob(f"file://{tmp_path}/reaped-away.bin")
        with pytest.raises(UnsupportedBlobScheme):
            await aread_blob("s3://co-temp-blobs/ab/cdef")


class _FakeGcsBlob:
    def __init__(self, calls, outcome):
        self._calls, self._outcome = calls, outcome

    def download_to_file(self, fh):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        fh.write(self._outcome)


class _FakeGcsClient:
    """Records (bucket, key) pairs; one outcome for every download."""

    def __init__(self, outcome):
        self.calls: list[tuple[str, str]] = []
        self._outcome = outcome

    def bucket(self, name):
        client = self

        class _Bucket:
            def blob(self, key):
                client.calls.append((name, key))
                return _FakeGcsBlob(client.calls, client._outcome)

        return _Bucket()


@pytest.fixture
def gcs(monkeypatch):
    """Install a fake GCS client; yields a factory taking the download outcome."""

    def _install(outcome):
        fake = _FakeGcsClient(outcome)
        monkeypatch.setattr(blobs_mod, "_gcs_client", lambda: fake)
        return fake

    return _install


class TestGcsArm:
    GS_URI = "gs://co-gcs-blobs/blobs/" + "ab" * 32 + ".bin"

    def test_downloads_through_the_spooled_temp_file(self, gcs):
        fake = gcs(b"<p>hi</p>")

        assert read_blob(self.GS_URI) == b"<p>hi</p>"
        # replicator#7: the key is FLAT — bucket from the authority, key the
        # path verbatim. Deriving anything (the file:// shard rule especially)
        # would be wrong; the URI is used as given.
        assert fake.calls == [("co-gcs-blobs", "blobs/" + "ab" * 32 + ".bin")]

    def test_not_found_is_reissuable(self, gcs):
        # Under this backend a 404 is the lifecycle rule having reaped the
        # blob — exactly the case a fresh content.fetch repairs.
        gcs(gcs_exceptions.NotFound("no such object"))

        with pytest.raises(BlobUnreadable):
            read_blob(self.GS_URI)

    def test_forbidden_is_permanent(self, gcs):
        # A missing or revoked grant stays broken until an operator acts;
        # re-fetching three times against it just burns origin requests.
        gcs(gcs_exceptions.Forbidden("no objectViewer"))

        with pytest.raises(UnsupportedBlobScheme):
            read_blob(self.GS_URI)

    def test_any_other_sdk_error_is_reissuable(self, gcs):
        # The wrapper's contract (CR-10): untyped failures land on the capped
        # side rather than escaping the apply task.
        gcs(gcs_exceptions.ServiceUnavailable("backend flapped"))

        with pytest.raises(BlobUnreadable):
            read_blob(self.GS_URI)

    def test_missing_bucket_or_key_is_permanent(self, gcs):
        gcs(b"")
        with pytest.raises(UnsupportedBlobScheme):
            read_blob("gs:///blobs/x.bin")
        with pytest.raises(UnsupportedBlobScheme):
            read_blob("gs://co-gcs-blobs")

    def test_unset_credential_is_permanent(self, monkeypatch):
        # The identity is GCS_BLOB_CREDENTIALS (singular BLOB) — deliberately
        # NOT GOOGLE_APPLICATION_CREDENTIALS, which is the wheelhouse SA.
        # Unset, no gs:// blob is readable by this process until an operator
        # acts: permanent, zero re-issues.
        monkeypatch.delenv(GCS_BLOB_CREDENTIALS_ENV, raising=False)
        monkeypatch.setattr(blobs_mod, "_gcs_client_cache", None)

        with pytest.raises(UnsupportedBlobScheme):
            read_blob(self.GS_URI)

    def test_an_unusable_credential_is_permanent(self, monkeypatch, tmp_path):
        # CR-17: a set-but-wrong path (or a malformed key) is the same operator
        # misconfig as an unset variable — construction never touches the
        # network, so the failure is deterministic. It must not burn the cap:
        # under the backend flip that is three wasted origin fetches per item,
        # fleet-wide, before every item settles FAILED.
        monkeypatch.setenv(GCS_BLOB_CREDENTIALS_ENV, str(tmp_path / "no-such-key.json"))
        monkeypatch.setattr(blobs_mod, "_gcs_client_cache", None)

        with pytest.raises(UnsupportedBlobScheme):
            read_blob(self.GS_URI)

        # Never cached: fixing the file is all recovery needs.
        assert blobs_mod._gcs_client_cache is None

    def test_the_client_is_built_once(self, monkeypatch, tmp_path):
        built = []
        monkeypatch.setenv(GCS_BLOB_CREDENTIALS_ENV, str(tmp_path / "key.json"))
        monkeypatch.setattr(blobs_mod, "_gcs_client_cache", None)
        monkeypatch.setattr(
            blobs_mod.storage.Client,
            "from_service_account_json",
            classmethod(lambda cls, path: built.append(path) or _FakeGcsClient(b"x")),
        )

        assert read_blob(self.GS_URI) == b"x"
        assert read_blob(self.GS_URI) == b"x"
        assert built == [str(tmp_path / "key.json")]


@pytest.mark.integration
class TestGcsLive:
    async def test_the_binding_answers_404_not_403(self):
        """A bogus key must come back NotFound, not Forbidden.

        This is the live proof the credential wiring works end to end: an
        unauthenticated or unbound principal gets 403 on a private bucket, so
        a 404 can only mean the request authenticated AND the objectViewer
        grant held. No object is created or needed.
        """
        if not os.environ.get(GCS_BLOB_CREDENTIALS_ENV):
            pytest.skip(f"{GCS_BLOB_CREDENTIALS_ENV} not set")

        with pytest.raises(BlobUnreadable):
            await aread_blob("gs://co-gcs-blobs/blobs/" + "0" * 64 + ".bin")
