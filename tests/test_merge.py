"""Tests for manual N→1 memory merge (engine.merge / service.merge).

Merge is the multi-input generalization of ``correct``: several memories become one,
the sources are *retired into history* (bi-temporally closed — never a hard delete,
AGENTS.md §3.2), and the new memory ``supersedes`` them. These tests pin the house
rules the write path must uphold: history preservation, the supersession chain, the
``merges`` links, the safety inheritance (untrusted/secret can't be laundered away,
protection can't be silently stripped), workspace confinement, and the compaction number.

Offline, ``numpy``-only — part of the CI offline gate.
"""
from __future__ import annotations

import pytest

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.service import MemoryService, ValidationError


def _svc():
    return MemoryService.create(":memory:")


def test_merge_retires_sources_without_deleting_and_records_chain():
    svc = _svc()
    a = svc.remember("Deploys happen Friday at 3pm.", workspace="w", mtype="semantic", title="A")
    b = svc.remember("We deploy Fridays around 15:00.", workspace="w", mtype="semantic", title="B")

    out = svc.merge([a["id"], b["id"]], "Deploys ship every Friday ~15:00.",
                    workspace="w", title="Deploy schedule")

    # Sources are retired (validity closed) but still present — never hard-deleted.
    for sid in (a["id"], b["id"]):
        r = svc.store.get_memory(sid)
        assert r is not None, "source must not be hard-deleted"
        assert r.valid_to is not None, "source validity must be closed"
    merged = svc.store.get_memory(out["id"])
    assert merged.valid_to is None and merged.expired_at is None, "merged memory must be live"

    # The supersession pointer + links make the relationship queryable.
    assert set(merged.metadata.get("supersedes") or []) == {a["id"], b["id"]}
    det = svc.inspect(out["id"], workspace="w")
    assert {a["id"], b["id"], out["id"]} <= {c["id"] for c in det["chain"]}
    assert sorted(link["relation"] for link in det["links"]) == ["merges", "merges"]


def test_merge_reports_compaction_number():
    svc = _svc()
    a = svc.remember("The primary database is Postgres 15.", workspace="w", mtype="semantic")
    b = svc.remember("We run PostgreSQL (v15) as the main datastore.", workspace="w", mtype="semantic")
    out = svc.merge([a["id"], b["id"]], "Primary DB: Postgres 15.", workspace="w")
    comp = out["compaction"]
    assert comp["units"] == 2
    assert comp["tokens_after"] <= comp["tokens_before"]
    assert comp["tokens_saved"] == max(0, comp["tokens_before"] - comp["tokens_after"])


def test_merge_removes_duplicates_from_live_recall():
    svc = _svc()
    a = svc.remember("The API rate limit is 100 requests per second.", workspace="w", mtype="semantic")
    b = svc.remember("Our API allows 100 req/s.", workspace="w", mtype="semantic")
    out = svc.merge([a["id"], b["id"]], "API rate limit: 100 req/s.", workspace="w")
    ids = {m["id"] for m in svc.recall("api rate limit", workspace="w", k=10)["memories"]}
    assert a["id"] not in ids and b["id"] not in ids, "retired sources must not surface in recall"
    assert out["id"] in ids


def test_merge_marks_untrusted_if_any_source_untrusted():
    """No laundering: a merge that includes untrusted content stays untrusted."""
    svc = _svc()
    a = svc.remember("A trusted, first-party fact.", workspace="w", mtype="semantic")
    b = svc.remember("A scraped, third-party claim.", workspace="w", mtype="semantic",
                     source="web", trusted=False)
    out = svc.merge([a["id"], b["id"]], "Combined.", workspace="w")
    assert out["trusted"] is False
    assert svc.store.get_memory(out["id"]).provenance.get("trusted") is False


def test_merge_inherits_most_restrictive_sensitivity():
    """A merge keeps the highest sensitivity of its sources (secret > sensitive > normal)."""
    svc = _svc()
    a = svc.remember("An ordinary fact.", workspace="w", mtype="semantic")
    b = svc.remember("A confidential fact.", workspace="w", mtype="semantic")
    # The write path defaults sensitivity to 'normal'; mark one source secret directly.
    svc.store.conn.execute("UPDATE memories SET sensitivity='secret' WHERE id=?", (b["id"],))
    svc.store.conn.commit()
    out = svc.merge([a["id"], b["id"]], "Combined.", workspace="w")
    assert out["sensitivity"] == "secret"
    assert svc.store.get_memory(out["id"]).sensitivity == "secret"


def test_merge_pins_result_if_any_source_pinned():
    """A merge can't silently strip protection: a pinned source pins the result."""
    svc = _svc()
    a = svc.remember("Load-bearing fact.", workspace="w", mtype="semantic")
    b = svc.remember("Related fact.", workspace="w", mtype="semantic")
    svc.pin(a["id"], workspace="w", pinned=True)
    out = svc.merge([a["id"], b["id"]], "Combined.", workspace="w")
    assert out["pinned"] is True
    assert svc.store.get_memory(out["id"]).pinned is True


def test_merge_rejects_cross_workspace():
    svc = _svc()
    a = svc.remember("fact in w1", workspace="w1", mtype="semantic")
    b = svc.remember("fact in w2", workspace="w2", mtype="semantic")
    with pytest.raises(ValidationError):
        svc.merge([a["id"], b["id"]], "x", workspace="w1")
    # And the w2 source is untouched (never read/retired across the boundary).
    assert svc.store.get_memory(b["id"]).valid_to is None


def test_merge_requires_two_distinct_sources():
    svc = _svc()
    a = svc.remember("only one", workspace="w", mtype="semantic")
    with pytest.raises(ValidationError):
        svc.merge([a["id"], a["id"]], "x", workspace="w")  # dedupes to a single id


def test_engine_merge_unions_keywords_and_takes_max_importance():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    a = eng.remember("alpha", workspace_id=wid, mtype=MemoryType.SEMANTIC,
                     keywords=["x", "y"], importance=0.2, resolve_conflicts=False)
    b = eng.remember("beta", workspace_id=wid, mtype=MemoryType.SEMANTIC,
                     keywords=["y", "z"], importance=0.9, resolve_conflicts=False)
    out = eng.merge([a, b], "merged content")
    rec = eng.store.get_memory(out["id"])
    assert set(rec.keywords) == {"x", "y", "z"}
    assert rec.importance == pytest.approx(0.9)


def test_engine_merge_missing_source_raises_keyerror():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    a = eng.remember("alpha", workspace_id=wid, mtype=MemoryType.SEMANTIC, resolve_conflicts=False)
    with pytest.raises(KeyError):
        eng.merge([a, "mem_does_not_exist"], "x")


def test_merge_writes_the_result_before_retiring_any_source():
    """Regression: a failing ``remember()`` used to leave every source retired.

    ``merge`` closed all sources first, so any error on the way back in — an unstorable
    scope, a bad session, a full disk — destroyed the inputs with nothing to replace
    them. That is unrecoverable loss from an operation whose entire contract is
    "retired into history, never a hard delete"."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    a = eng.remember("alpha", workspace_id=wid, mtype=MemoryType.SEMANTIC,
                     resolve_conflicts=False)
    b = eng.remember("beta", workspace_id=wid, mtype=MemoryType.SEMANTIC,
                     resolve_conflicts=False)

    def boom(*_args, **_kw):
        raise RuntimeError("simulated write failure")

    eng.remember = boom
    with pytest.raises(RuntimeError):
        eng.merge([a, b], "alpha and beta")

    for sid in (a, b):
        assert eng.store.get_memory(sid).valid_to is None, "source must survive a failed merge"


def test_merge_across_repos_lands_at_workspace_scope():
    """The documented cross-repo repro: ``repo_id`` resolves to None while the inherited
    scope stays ``repo``, which ``remember`` rejects. It now widens to ``workspace``."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    one = eng.store.get_or_create_repo(wid, "one")
    two = eng.store.get_or_create_repo(wid, "two")
    a = eng.remember("Alpha ships from repo one.", workspace_id=wid, repo_id=one,
                     scope=Scope.REPO, mtype=MemoryType.SEMANTIC, resolve_conflicts=False)
    b = eng.remember("Beta ships from repo two.", workspace_id=wid, repo_id=two,
                     scope=Scope.REPO, mtype=MemoryType.SEMANTIC, resolve_conflicts=False)

    out = eng.merge([a, b], "Alpha and Beta ship from separate repos.")

    merged = eng.store.get_memory(out["id"])
    assert merged.scope == Scope.WORKSPACE and merged.repo_id is None
    assert merged.valid_to is None and merged.expired_at is None
    for sid in (a, b):
        assert eng.store.get_memory(sid).valid_to is not None   # retired, not deleted
    relations = sorted(link["relation"] for link in eng.store.get_links(out["id"]))
    assert relations == ["merges", "merges"]


def test_merge_is_audited_on_both_sides():
    svc = _svc()
    a = svc.remember("fact one", workspace="w", mtype="semantic")
    b = svc.remember("fact two", workspace="w", mtype="semantic")
    out = svc.merge([a["id"], b["id"]], "merged", workspace="w", reason="dedupe")
    actions = [r["action"] for r in svc.store.conn.execute(
        "SELECT action FROM audit WHERE target=?", (out["id"],)).fetchall()]
    assert "merge" in actions
    # each source carries a merge audit row pointing at the result
    for sid in (a["id"], b["id"]):
        details = [r["detail"] for r in svc.store.conn.execute(
            "SELECT detail FROM audit WHERE target=? AND action='merge'", (sid,)).fetchall()]
        assert any(out["id"] in (d or "") for d in details)
