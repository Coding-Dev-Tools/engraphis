"""Thread-race regression tests for the license cache and device-id generation
(commit 7b62157 "serialize the process license cache and device-id generation").

Both fixes serialize shared *process* state. Without them, a first-run race mints two
different device ids (registering the same machine twice → burns a Team seat), or a
``current_license()`` read returns ``None`` when an ``invalidate_cache()`` lands between
its "cached is not None" check and its return. These prove the locks hold under real
concurrent threads — the coverage the shipping commit lacked.
"""
from concurrent.futures import ThreadPoolExecutor
import threading


def test_machine_id_is_stable_under_concurrent_first_generation(monkeypatch, tmp_path):
    """N threads racing a fresh (uncached, unpersisted) device id must all get the SAME
    id and persist exactly one file — otherwise a Team deployment burns extra seats. This
    is the race the ``_machine_id_lock`` + ``O_EXCL`` atomic create close."""
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


def test_current_license_never_returns_none_under_concurrent_invalidation(
        monkeypatch, tmp_path):
    """The ``_cache_lock`` closes a torn read: without it, an ``invalidate_cache()`` landing
    between ``current_license()``'s "cached is not None" check and its return could hand back
    ``None``. Hammer reads while another thread invalidates; every read must be a ``License``,
    never ``None``, never an exception. Uses the offline free tier — no network or key setup.
    """
    from engraphis import licensing as lic

    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "absent.key")
    lic.current_license(refresh=True)                   # prime the cache (free tier, offline)

    errors = []
    results = []
    iters = 400

    def reader():
        for _ in range(iters):
            try:
                results.append(lic.current_license())
            except Exception as exc:                    # noqa: BLE001 — any raise is a failure
                errors.append(repr(exc))

    def invalidator():
        for _ in range(iters):
            lic.invalidate_cache()

    threads = [threading.Thread(target=reader) for _ in range(6)]
    threads += [threading.Thread(target=invalidator) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors[:3]
    assert results and all(r is not None for r in results)
