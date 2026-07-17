"""Regression: the v1 background decay pass must be idempotent / frequency-independent.

The old ``apply_decay_to_all`` reapplied a fixed days-since-access factor to the
already-decayed stored stability on EVERY call, so the ~60s consciousness loop compounded
decay and collapsed every memory's stability to the floor within minutes. The fix anchors
decay on ``last_decay`` (advanced each pass), so a given interval is decayed exactly once.
"""
import threading

from engraphis.config import settings
from engraphis.stores import get_conn, init_db, now_ts
from engraphis.stores import vectors as mem_store


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "decay.db"))
    # The v1 store keeps a process-global thread-local connection; reset it so this test
    # binds to its own DB rather than a stale conn left by an earlier test file.
    monkeypatch.setattr("engraphis.stores._local", threading.local())
    init_db()


def _insert(namespace, doc_id, stability, last_access):
    conn = get_conn()
    conn.execute(
        "INSERT INTO memories (namespace, document_id, title, content, metadata, "
        "created_at, updated_at, last_access, access_count, stability, surprise, "
        "memory_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (namespace, doc_id, "", "content", "{}", last_access, last_access, last_access,
         0, stability, 1.0, "semantic"))
    conn.commit()


def _stability(namespace, doc_id):
    return get_conn().execute(
        "SELECT stability FROM memories WHERE namespace=? AND document_id=?",
        (namespace, doc_id)).fetchone()["stability"]


def test_decay_is_frequency_independent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    now = now_ts()
    # Two identical memories last accessed 10 days ago, in separate namespaces so each can
    # be decayed a different number of times.
    _insert("freq", "m", 5.0, now - 10 * 86400)
    _insert("once", "m", 5.0, now - 10 * 86400)

    for _ in range(50):                      # the compounding scenario (loop every tick)
        mem_store.apply_decay_to_all("freq", 7.0)
    mem_store.apply_decay_to_all("once", 7.0)  # the same elapsed time, applied once

    s_freq = _stability("freq", "m")
    s_once = _stability("once", "m")
    # 50 rapid passes must land at the SAME stability as a single pass over the same
    # elapsed interval — not 50x compounded down to the floor.
    assert abs(s_freq - s_once) < 0.05, (s_freq, s_once)
    # ~10 days at a 7-day half-life leaves 5.0 * 0.5**(10/7) ≈ 1.86, nowhere near the floor.
    assert s_freq > 1.0


def test_reinforced_memory_is_not_decayed_that_interval(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    now = now_ts()
    _insert("ns", "hot", 5.0, now - 10 * 86400)
    # First pass anchors + decays the stale memory once.
    mem_store.apply_decay_to_all("ns", 7.0)
    decayed = _stability("ns", "hot")
    assert decayed < 5.0
    # Simulate reinforcement: the memory is accessed now (last_access moves past the anchor).
    conn = get_conn()
    conn.execute("UPDATE memories SET last_access=? WHERE namespace=? AND document_id=?",
                 (now_ts(), "ns", "hot"))
    conn.commit()
    # A subsequent pass must NOT decay it further — it was just accessed.
    mem_store.apply_decay_to_all("ns", 7.0)
    assert abs(_stability("ns", "hot") - decayed) < 1e-9
