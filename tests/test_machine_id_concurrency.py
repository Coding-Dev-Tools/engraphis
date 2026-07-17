"""Thread-race regression test for device-id (machine_id) first-run generation
(commit 7b62157 "serialize the process license cache and device-id generation").

Without the ``_machine_id_lock`` + ``O_EXCL`` atomic create, N threads racing a fresh,
unpersisted id each mint a DIFFERENT uuid — registering the same device twice and burning a
Team seat. This proves they converge on ONE id and persist exactly one file. (The
license-cache half of 7b62157 is exercised by the concurrency test in
tests/test_online_only_enforcement.py.)
"""
from concurrent.futures import ThreadPoolExecutor
import threading


def test_machine_id_is_stable_under_concurrent_first_generation(monkeypatch, tmp_path):
    from engraphis import cloud_license

    mid_file = tmp_path / "sub" / "machine_id"          # parent dir does not exist yet
    monkeypatch.setattr(cloud_license, "_MACHINE_ID_FILE", mid_file)
    cloud_license._machine_id_cache.clear()             # force a real first-run generation

    barrier = threading.Barrier(16)

    def get_id(_):
        barrier.wait()                                  # release all threads together
        return cloud_license.machine_id()

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            ids = list(pool.map(get_id, range(16)))
        assert len(set(ids)) == 1, "concurrent first-run generation minted more than one id"
        assert ids[0]
        assert mid_file.read_text(encoding="utf-8").strip() == ids[0]
    finally:
        cloud_license._machine_id_cache.clear()         # don't leak the tmp id to other tests
