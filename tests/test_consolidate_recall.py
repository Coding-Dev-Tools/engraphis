"""Consolidation ↔ recall wiring.

A consolidated digest/profile is a durable summary of the raw memories it distills,
so it should be *preferentially* retrievable (a small deterministic bonus) and its
source memories should be citable evidence (ids, never duplicated bodies).
"""
from __future__ import annotations

from engraphis.backends import DeterministicEmbedder
from engraphis.backends.reranker import IdentityReranker
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core.consolidate import consolidate
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, SearchFilter
from engraphis.core.recall import (
    CONSOLIDATION_BONUS,
    CONSOLIDATION_SOURCES,
    RecallEngine,
    _consolidation_evidence,
    _consolidated_source,
)
from engraphis.core.store import Store


class _SemanticTestEmbedder(DeterministicEmbedder):
    """Test double that opts into vector semantics without a model download."""

    supports_semantic_search = True
    embedding_mode = "semantic"


def _recall_engine(store):
    emb = _SemanticTestEmbedder(256)
    return RecallEngine(store, emb, get_vector_index(store, dim=256, prefer="numpy"),
                        IdentityReranker())


def _engine_with_repeats():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    texts = [
        "Build failed on the flaky network integration test in CI run 101.",
        "Build failed on the flaky network integration test in CI run 202.",
        "Build failed on the flaky network integration test in CI run 303.",
    ]
    for text in texts:
        eng.remember(text, workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                     resolve_conflicts=False)
    return eng, wid, rid


def test_digest_receives_bonus_over_its_raw_episodes():
    """A digest retrieved alongside its sources ranks above them (deterministically)."""
    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    digest_id = report["digests_created"][0]["id"]
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for text in (
        "Build failed on the flaky network integration test in CI run 101.",
        "Build failed on the flaky network integration test in CI run 202.",
        "Build failed on the flaky network integration test in CI run 303.",
        "Build failed on the flaky network integration test in CI run 404.",
        "Build failed on the flaky network integration test in CI run 505.",
        "Build failed on the flaky network integration test in CI run 606.",
    ):
        eng.remember(text, workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                     resolve_conflicts=False)
    r = consolidate(eng, workspace_id=wid, repo_id=rid)
    digest_id = r["digests_created"][0]["id"]
    dig = eng.store.get_memory(digest_id)
    assert dig.metadata["provenance"]["source"] in CONSOLIDATION_SOURCES

    res = eng.recall_engine.recall(
        "flaky network integration test build failure",
        SearchFilter(workspace_id=wid, repo_id=rid),
        k=6,
        reinforce=False,
    )
    ids = [c["id"] for c in res.chunks]
    assert digest_id in ids
    top = max(c["score"] for c in res.chunks)
    digest_score = next(c["score"] for c in res.chunks if c["id"] == digest_id)
    episode_scores = [c["score"] for c in res.chunks if c["id"] != digest_id]
    assert episode_scores
    assert digest_score == top                          # digest outranks all its episodes
    assert digest_score >= max(episode_scores) + CONSOLIDATION_BONUS - 1e-12

    # Deterministic across repeated runs.
    runs = [[c["id"] for c in eng.recall_engine.recall(
        "flaky network integration test build failure",
        SearchFilter(workspace_id=wid, repo_id=rid), k=6, reinforce=False,
    ).chunks] for _ in range(3)]
    assert all(run == runs[0] for run in runs)


def test_structured_digest_is_consolidated_source():
    """Schema-distilled facts (provenance.source='structured_consolidation') count."""
    record_meta = {"provenance": {"source": "structured_consolidation", "trusted": True}}
    record = type("R", (), {"provenance": record_meta["provenance"],
                            "metadata": record_meta, "id": "m1"})()
    assert _consolidated_source(record) is True


def test_digest_exposes_its_source_ids_as_citable_evidence():
    """The recall result surfaces the digest's ``consolidates`` ids without bodies."""
    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    digest_id = report["digests_created"][0]["id"]
    sources = set(report["digests_created"][0]["consolidates"])

    res = eng.recall_engine.recall(
        "flaky network integration test",
        SearchFilter(workspace_id=wid, repo_id=rid),
        k=8,
        reinforce=False,
    )
    chunk = next(c for c in res.chunks if c["id"] == digest_id)
    assert set(chunk["consolidation_source_ids"]) == sources

    metadata = res.source_metadata.get(digest_id, {})
    assert set(metadata["consolidation_source_ids"]) == sources

    # Evidence is ids, never duplicated bodies: the digest's chunk packs only the
    # digest's own content, and its source episodes are not packed in its place.
    digest_chunk = next(c for c in res.chunks if c["id"] == digest_id)
    assert digest_chunk["content"] == eng.store.get_memory(digest_id).content
    assert all("flaky" not in str(chunk["consolidation_source_ids"]) for chunk in res.chunks)


def test_recall_resolves_consolidation_evidence_once(monkeypatch):
    from engraphis.core.interfaces import MemoryRecord, Scope

    store = Store(":memory:")
    eng = _recall_engine(store)
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    approved = {"source": "test", "trusted": True, "review_state": "approved"}
    source_ids = []
    for run in (101, 202, 303):
        content = f"Build failed on the flaky network integration test in CI run {run}."
        source_ids.append(store.add_memory(MemoryRecord(
            id="",
            content=content,
            mtype=MemoryType.EPISODIC,
            scope=Scope.REPO,
            workspace_id=wid,
            repo_id=rid,
            metadata={"provenance": approved},
            provenance=approved,
            embedding=eng.embedder.embed([content])[0],
        )))
    digest_content = "Flaky network integration test failures repeat in CI."
    digest_id = store.add_memory(MemoryRecord(
        id="",
        content=digest_content,
        mtype=MemoryType.SEMANTIC,
        scope=Scope.REPO,
        workspace_id=wid,
        repo_id=rid,
        metadata={"provenance": {
            "source": "consolidation",
            "trusted": True,
            "review_state": "approved",
            "consolidates": source_ids,
        }},
        provenance={
            "source": "consolidation",
            "trusted": True,
            "review_state": "approved",
            "consolidates": source_ids,
        },
        embedding=eng.embedder.embed([digest_content])[0],
    ))
    for source_id in source_ids:
        store.add_link(digest_id, source_id, "consolidates")
    expected_sources = set(source_ids)
    link_calls = []
    memory_calls = []
    real_get_links = store.get_links
    real_get_memory = store.get_memory

    def recording_get_links(memory_id, *, flt=None):
        link_calls.append(memory_id)
        return real_get_links(memory_id, flt=flt)

    def recording_get_memory(memory_id):
        memory_calls.append(memory_id)
        return real_get_memory(memory_id)

    monkeypatch.setattr(store, "get_links", recording_get_links)
    monkeypatch.setattr(store, "get_memory", recording_get_memory)
    result = eng.recall(
        "flaky network integration test",
        SearchFilter(workspace_id=wid, repo_id=rid),
        k=4,
        reinforce=False,
    )
    chunk = next(item for item in result.chunks if item["id"] == digest_id)

    assert set(chunk["consolidation_source_ids"]) == expected_sources
    assert set(result.source_metadata[digest_id]["consolidation_source_ids"]) == (
        expected_sources
    )
    assert link_calls == [digest_id]
    assert set(memory_calls) == expected_sources
    assert len(memory_calls) == len(expected_sources)
    store.close()


def test_consolidation_evidence_stays_inside_the_active_repo_scope():
    """Provenance source ids must not cross a repo recall boundary."""
    from engraphis.core.interfaces import MemoryRecord, Scope

    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    repo_a = store.get_or_create_repo(wid, "repo-a")
    repo_b = store.get_or_create_repo(wid, "repo-b")
    source_a = store.add_memory(MemoryRecord(
        id="",
        content="repo A evidence",
        mtype=MemoryType.EPISODIC,
        scope=Scope.REPO,
        workspace_id=wid,
        repo_id=repo_a,
    ))
    source_b = store.add_memory(MemoryRecord(
        id="",
        content="repo B evidence",
        mtype=MemoryType.EPISODIC,
        scope=Scope.REPO,
        workspace_id=wid,
        repo_id=repo_b,
    ))
    digest = store.add_memory(MemoryRecord(
        id="",
        content="repo A digest",
        mtype=MemoryType.SEMANTIC,
        scope=Scope.REPO,
        workspace_id=wid,
        repo_id=repo_a,
        provenance={
            "source": "consolidation",
            "consolidates": [source_a, source_b],
        },
    ))
    store.add_link(digest, source_a, "consolidates")

    evidence = _consolidation_evidence(
        store.get_memory(digest),
        store=store,
        flt=SearchFilter(workspace_id=wid, repo_id=repo_a),
    )

    assert evidence == [source_a]


def test_non_consolidated_memory_is_unchanged(monkeypatch):
    """Ordinary memories get no bonus and no evidence field."""
    from engraphis.core.interfaces import MemoryRecord, Scope

    store = Store(":memory:")
    eng = _recall_engine(store)
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="",
        content="pnpm is our package manager.",
        mtype=MemoryType.SEMANTIC,
        scope=Scope.REPO,
        workspace_id=wid,
        repo_id=rid,
        metadata={"provenance": {"source": "test", "trusted": True,
                                 "review_state": "approved"}},
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
        importance=0.5,
        embedding=_SemanticTestEmbedder(256).embed(["pnpm is our package manager."])[0],
    ))
    link_calls = []
    real_get_links = store.get_links

    def recording_get_links(memory_id, *, flt=None):
        link_calls.append(memory_id)
        return real_get_links(memory_id, flt=flt)

    monkeypatch.setattr(store, "get_links", recording_get_links)
    res = eng.recall("package manager", SearchFilter(workspace_id=wid, repo_id=rid), k=1,
                     reinforce=False)
    assert res.count == 1
    chunk = res.chunks[0]
    assert chunk["id"] == mid
    assert chunk["consolidation_source_ids"] == []
    assert res.source_metadata.get(mid, {}).get("consolidation_source_ids") is None
    assert link_calls == []
