from src.core.sources.scratch import (
    allocate_revision_id,
    rename_scratch_to_canonical,
    scratch_path_for,
    write_scratch_bytes,
)


def test_allocate_revision_id_returns_ulid_string():
    uid = allocate_revision_id()
    assert isinstance(uid, str)
    assert len(uid) == 26


def test_write_scratch_bytes_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    uid = allocate_revision_id()
    path = write_scratch_bytes(uid, b"hello world")
    assert path.exists()
    assert path.read_bytes() == b"hello world"
    assert path.name == f"{uid}.bin"


def test_rename_to_canonical_when_ids_differ(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    old_uid = allocate_revision_id()
    new_uid = allocate_revision_id()
    old_path = write_scratch_bytes(old_uid, b"data")
    new_path = rename_scratch_to_canonical(old_uid, new_uid)
    assert not old_path.exists()
    assert new_path.exists()
    assert new_path.name == f"{new_uid}.bin"


def test_rename_noop_when_ids_match(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    uid = allocate_revision_id()
    original = write_scratch_bytes(uid, b"data")
    returned = rename_scratch_to_canonical(uid, uid)
    assert returned == original
    assert returned.exists()


def test_rename_target_exists_unlinks_source(tmp_path, monkeypatch):
    """If canonical file already exists, source is unlinked and canonical returned."""
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    old_uid = allocate_revision_id()
    new_uid = allocate_revision_id()
    write_scratch_bytes(old_uid, b"old")
    write_scratch_bytes(new_uid, b"new")  # pre-existing canonical
    returned = rename_scratch_to_canonical(old_uid, new_uid)
    assert returned.read_bytes() == b"new"  # canonical preserved, not overwritten
    assert not scratch_path_for(old_uid).exists()
