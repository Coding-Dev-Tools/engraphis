from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.store import Store


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
