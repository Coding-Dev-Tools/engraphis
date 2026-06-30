from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope


def test_engine_remember_and_recall():
    eng = MemoryEngine.create(":memory:")          # offline defaults
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("We deploy with GitHub Actions to AWS ECS.", workspace_id=wid, repo_id=rid,
                 title="deployment", importance=0.8)
    eng.remember("Lunch is usually around noon.", workspace_id=wid, repo_id=rid)
    res = eng.recall("how do we deploy?", workspace_id=wid, k=2)
    assert res.count >= 1
    assert "actions" in res.context.lower() or "aws" in res.context.lower()


def test_engine_falls_back_to_numpy_index_offline():
    eng = MemoryEngine.create(":memory:")
    # sqlite-vec is unavailable in the sandbox → factory falls back to NumPy.
    assert isinstance(eng.index, NumpyVectorIndex)


def test_engine_respects_memory_type_and_scope():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("How to add a migration: edit models, run alembic revision.",
                       workspace_id=wid, repo_id=rid, mtype=MemoryType.PROCEDURAL, scope=Scope.REPO)
    rec = eng.store.get_memory(mid)
    assert rec.mtype == MemoryType.PROCEDURAL and rec.scope == Scope.REPO
