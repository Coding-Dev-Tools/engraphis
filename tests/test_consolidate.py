import time

from engraphis.core.consolidate import consolidate
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, SearchFilter


def _engine_with_repeats():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    texts = [
        "Build failed on the flaky network integration test in CI run 101.",
        "Build failed on the flaky network integration test in CI run 202.",
        "Build failed on the flaky network integration test in CI run 303.",
        "Design review scheduled for the onboarding flow mockups.",   # unrelated
    ]
    for t in texts:
        eng.remember(t, workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                     resolve_conflicts=False)
    return eng, wid, rid


def test_consolidate_distills_recurring_episodes_into_semantic_digest():
    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert report["clusters_found"] == 1
    assert len(report["digests_created"]) == 1
    digest_id = report["digests_created"][0]["id"]
    digest = eng.store.get_memory(digest_id)
    assert digest.mtype == MemoryType.SEMANTIC
    assert "flaky" in digest.content or "network" in digest.content
    assert digest.metadata["provenance"]["source"] == "consolidation"
    links = eng.store.get_links(digest_id)
    assert sum(1 for link in links if link["relation"] == "consolidates") == 3


def test_consolidate_is_idempotent():
    eng, wid, rid = _engine_with_repeats()
    first = consolidate(eng, workspace_id=wid, repo_id=rid)
    second = consolidate(eng, workspace_id=wid, repo_id=rid)
    assert len(first["digests_created"]) == 1
    assert len(second["digests_created"]) == 0
    assert second["skipped_already_consolidated"] >= 1


def test_consolidate_dry_run_changes_nothing():
    eng, wid, rid = _engine_with_repeats()
    before = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    report = consolidate(eng, workspace_id=wid, repo_id=rid, dry_run=True)
    after = len(eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100))
    assert report["dry_run"] is True
    assert before == after
    assert report["digests_created"] and "would_consolidate" in report["digests_created"][0]


def test_consolidate_archives_decayed_transients_but_not_pinned():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    stale = eng.remember("Currently blocked on CI quota.", workspace_id=wid, repo_id=rid,
                         mtype=MemoryType.WORKING)
    pinned = eng.remember("Blocked on the vendor contract renewal.", workspace_id=wid,
                          repo_id=rid, mtype=MemoryType.WORKING)
    eng.pin(pinned)
    # Age both far past any plausible retention: tiny stability, ancient last_access.
    old = time.time() - 90 * 86400
    for mid in (stale, pinned):
        eng.store.conn.execute(
            "UPDATE memories SET stability=0.5, last_access=? WHERE id=?", (old, mid))
    eng.store.conn.commit()

    report = consolidate(eng, workspace_id=wid, repo_id=rid)
    archived_ids = {a["id"] for a in report["archived"]}
    assert stale in archived_ids
    assert pinned not in archived_ids
    live = {m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid), limit=100)}
    assert stale not in live                     # left the live view...
    assert eng.store.get_memory(stale) is not None   # ...but never hard-deleted
    assert pinned in live


def test_consolidate_uses_llm_summary_when_available():
    class FakeLLM:
        def chat(self, messages, system=None, **kw):
            return "CI is flaky on the network integration test; treat failures as retryable."

    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, llm=FakeLLM())
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert digest.content.startswith("CI is flaky")


def test_consolidate_llm_failure_falls_back_to_deterministic():
    class BrokenLLM:
        def chat(self, messages, system=None, **kw):
            raise RuntimeError("provider down")

    eng, wid, rid = _engine_with_repeats()
    report = consolidate(eng, workspace_id=wid, repo_id=rid, llm=BrokenLLM())
    digest = eng.store.get_memory(report["digests_created"][0]["id"])
    assert "Recurring pattern" in digest.content
