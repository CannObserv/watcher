from ulid import ULID

from src.core.sources.scratch import scratch_path_for, write_scratch_bytes


def test_write_scratch_bytes_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    uid = str(ULID())
    path = write_scratch_bytes(uid, b"hello world")
    assert path.exists()
    assert path.read_bytes() == b"hello world"
    assert path.name == f"{uid}.bin"
    assert scratch_path_for(uid) == path
