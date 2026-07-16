from pathlib import Path

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.vector_sqlitevec import _cosine_from_l2
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.store import Store
from scripts.repair_embed_dim import repair


def test_search_ranks_relevant_memory_first():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=256)
    index = NumpyVectorIndex(store)

    texts = {
        "pm": "We standardized on pnpm as the package manager for all frontend repos.",
        "sky": "The afternoon sky over the harbor was a pale shade of blue.",
    }
    ids = {}
    for tag, text in texts.items():
        vec = emb.embed([text])[0]
        ids[tag] = store.add_memory(MemoryRecord(id="", content=text, scope=Scope.REPO,
                                                 workspace_id=wid, repo_id=rid, embedding=vec))

    hits = index.search(emb.embed(["which package manager do we use?"])[0], k=2)
    assert hits[0][0] == ids["pm"]
    assert hits[0][1] >= hits[1][1]
    store.close()


def test_search_skips_vectors_from_other_embedding_dimensions():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=384)
    index = NumpyVectorIndex(store)
    matching = store.add_memory(MemoryRecord(
        id="", content="matching vector", workspace_id=wid, repo_id=rid,
        embedding=emb.embed(["matching vector"])[0]))
    store.add_memory(MemoryRecord(
        id="", content="legacy vector", workspace_id=wid, repo_id=rid,
        embedding=DeterministicEmbedder(dim=256).embed(["legacy vector"])[0]))

    hits = index.search(emb.embed(["matching vector"])[0], k=5)

    assert [memory_id for memory_id, _score in hits] == [matching]
    store.close()


def test_timeline_skips_legacy_dimension_without_losing_lexical_results():
    engine = MemoryEngine.create(":memory:", embed_model=None, embed_dim=384)
    wid = engine.store.get_or_create_workspace("w")
    rid = engine.store.get_or_create_repo(wid, "r")
    mid = engine.remember(
        "durable migration fact", workspace_id=wid, repo_id=rid,
        resolve_conflicts=False)
    engine.store.put_vector(
        mid, DeterministicEmbedder(dim=256).embed(["durable migration fact"])[0])
    engine.store.conn.commit()

    results = engine.timeline("durable migration", workspace_id=wid, repo_id=rid)

    assert [record.id for record in results] == [mid]
    engine.store.close()


def test_repair_uses_active_dimension_and_creates_backup(tmp_path):
    db_path = tmp_path / "mixed.db"
    store = Store(str(db_path))
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = store.add_memory(MemoryRecord(
        id="", content="legacy vector", workspace_id=wid, repo_id=rid,
        embedding=DeterministicEmbedder(dim=256).embed(["legacy vector"])[0]))
    store.close()

    result = repair(str(db_path), model_name="", dim=384)

    assert result["repaired"] == 1
    assert result["by_dim"] == {384: 1}
    assert Path(result["backup"]).is_file()
    repaired = Store(str(db_path))
    row = repaired.conn.execute(
        "SELECT dim, model FROM mem_vectors WHERE id=?", (mid,)).fetchone()
    assert (row["dim"], row["model"]) == (384, "deterministic")
    repaired.close()


def test_delete_removes_from_index():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=128)
    index = NumpyVectorIndex(store)
    vec = emb.embed(["hello world"])[0]
    mid = store.add_memory(MemoryRecord(id="", content="hello world", workspace_id=wid,
                                        repo_id=rid, embedding=vec))
    assert index.search(vec, k=1)[0][0] == mid
    index.delete([mid])
    assert index.search(vec, k=1) == []
    store.close()


def test_sqlitevec_l2_distance_converts_to_cosine_similarity():
    assert _cosine_from_l2(0.0) == 1.0
    assert abs(_cosine_from_l2(2 ** 0.5)) < 1e-12
    assert _cosine_from_l2(2.0) == -1.0
