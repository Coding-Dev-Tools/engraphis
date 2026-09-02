"""Tests for the optional ``ENGRAPHIS_RECALL_ARM_CANDIDATE_K`` latency knob.

PR #171 widened the prompt-only first arm to ``candidate_k + min(250, candidate_k*3)``
so a 49-fact corpus pays ~5x more matrix-vector cost on the new k=50 default.  The
opt-in ``ENGRAPHIS_RECALL_ARM_CANDIDATE_K`` env var (and the matching constructor
kwarg ``arm_candidate_k_cap=``) lets an operator clamp that first-page widening
*and* the second-page ceiling for latency-sensitive deployments.

Default behavior (no env, no kwarg) is unchanged.
"""
from __future__ import annotations

import time

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.interfaces import MemoryRecord, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store


class _SemanticTestEmbedder(DeterministicEmbedder):
    supports_semantic_search = True
    embedding_mode = "semantic"


def _add(store, emb, wid, rid, text, **kw):
    provenance = dict(kw.get("provenance") or {
        "source": "test", "trusted": True, "review_state": "approved",
    })
    if provenance.get("trusted") is True:
        provenance.setdefault("review_state", "approved")
    kw["provenance"] = provenance
    return store.add_memory(MemoryRecord(
        id="", content=text, workspace_id=wid, repo_id=rid,
        embedding=emb.embed([text])[0], **kw,
    ))


class _RecordingIndex:
    """Vector-index double that records every arm size it was queried with."""

    def __init__(self, hit_count: int | None = None, real_ids: list[str] | None = None):
        """If ``hit_count`` is set, the index always returns exactly that many
        hits per call (up to ``k``). ``None`` (default) returns the synthetic
        up-to-4 series; ``0`` returns nothing; any positive int returns that
        many. ``real_ids`` (if given) replaces the synthetic id prefix so the
        returned ids resolve to real ``MemoryRecord`` rows in the store --
        otherwise the prompt-eligibility filter discards them and the
        escalation loop is short-circuited on empty recs.
        """
        self.requested: list[int] = []
        self.records: list[tuple[str, float]] = []
        self._hit_count = hit_count
        self._real_ids = list(real_ids) if real_ids else None

    def search(self, query, k, *, filter=None):
        self.requested.append(int(k))
        if self._hit_count == 0:
            return []
        cap = min(k, 4) if self._hit_count is None else min(k, self._hit_count)
        if self._real_ids is not None:
            return [(self._real_ids[i % len(self._real_ids)], float(k - i))
                    for i in range(cap)]
        return [(f"mem_{i}", float(k - i)) for i in range(cap)]


def test_arm_candidate_k_cap_default_is_none(monkeypatch):
    """Without the env var or kwarg the cap is unset and PR #171 is preserved."""
    monkeypatch.delenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", raising=False)
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._arm_candidate_k_cap is None


def test_arm_candidate_k_cap_reads_env_var(monkeypatch):
    """Operator-set env var populates the cap; whitespace and bad values are ignored."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", "  50  ")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._arm_candidate_k_cap == 50

    monkeypatch.setenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", "not-a-number")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._arm_candidate_k_cap is None


def test_arm_candidate_k_cap_constructor_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", "50")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker(), arm_candidate_k_cap=64)
    assert eng._arm_candidate_k_cap == 64


def test_arm_candidate_k_cap_clamps_first_arm(monkeypatch):
    """With cap=50, k=50 prompt-only first arm is 50 (was 200)."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", "50")
    index = _RecordingIndex()
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       index, IdentityReranker())
    store = eng.store
    wid = store.get_or_create_workspace("w")
    for i in range(60):
        _add(store, eng.embedder, wid, None, f"fact {i}")

    result = eng.recall("fact 5", SearchFilter(workspace_id=wid), k=50,
                        candidate_k=50, prompt_only=True)

    # First arm is clamped to 50; without the cap it would be 200.
    assert index.requested[0] == 50
    # candidate_k_used reflects the actual first-page widening.
    assert result.candidate_k_used == 50
    # The result must still be non-empty: the cap must not regress recall on
    # a trusted-only corpus.
    assert result.count >= 1


def test_arm_candidate_k_cap_clamps_ceiling_when_first_page_insufficient(monkeypatch):
    """The second page must also be clamped so the escalation loop does not
    silently undo the savings by jumping to PROMPT_ONLY_MIN_CANDIDATES."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", "8")
    # Build an engine whose only enabled arm is the vector arm, so the
    # lexical/graph/code arms cannot pad the prompt-eligible record set
    # and short-circuit the ceiling path. The recording index returns
    # exactly 1 hit per call (less than the prompt_target of 2 below and
    # less than arm_candidate_k=4 so can_expand is True), so the
    # escalation loop is forced into the arm_candidate_k =
    # candidate_ceiling branch.
    from engraphis.core.retrieval_policy import ProfileConfig
    vector_only = ProfileConfig(
        name="vector-only-test", vector=True, lexical=False, graph=False, code=False
    )
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    store = eng.store
    wid = store.get_or_create_workspace("w")
    real_ids = []
    for i in range(20):
        real_ids.append(_add(store, eng.embedder, wid, None, f"fact {i}"))

    index = _RecordingIndex(hit_count=4, real_ids=real_ids)
    eng.index = index

    eng.recall("zzz-unmatched-query-zzz", SearchFilter(workspace_id=wid), k=8,
               candidate_k=1, prompt_only=True, arm_config=vector_only)

    # First arm: 1 + min(250, 1*3) = 4 (clamped at min(8, 4) = 4, then
    # floored at candidate_k=1, so 4). With the cap, the second-page
    # ceiling is min(256, 8) = 8. Without the cap the index would have
    # been queried with [4, 256]. The recording index returns 4 hits per
    # call (>= arm_candidate_k=4 so can_expand=True; < prompt_target=8
    # so the loop is forced to escalate to the ceiling).
    assert index.requested, "index was never queried -- test setup is broken"
    assert index.requested[0] == 4, (
        f"first arm must be 4 (cap=8, candidate_k=1, prompt_only), "
        f"got {index.requested[0]}"
    )
    assert all(requested <= 8 for requested in index.requested), (
        f"every index query must respect the cap=8 ceiling, "
        f"got {index.requested}"
    )
    assert max(index.requested) <= 8
    # Sanity: the loop must have actually escalated, i.e. it must have
    # queried the index at least twice. If it queried only once, the
    # first arm satisfied prompt_target and the ceiling-clamp code path
    # was not exercised -- which is the vacuous case this test guards
    # against.
    assert len(index.requested) >= 2, (
        f"ceiling clamp must be exercised via the escalation loop; "
        f"only {len(index.requested)} index queries were recorded -- "
        f"the first arm already satisfied prompt_target and the cap "
        f"code path is not being run"
    )


def test_arm_candidate_k_cap_floor_protects_one_fact_corpus(monkeypatch):
    """The cap must not shrink the first arm below the caller's requested
    candidate_k — that would silently under-search a one-fact scope."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", "2")
    index = _RecordingIndex()
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       index, IdentityReranker())
    store = eng.store
    wid = store.get_or_create_workspace("w")
    for i in range(20):
        _add(store, eng.embedder, wid, None, f"fact {i}")

    eng.recall("fact 0", SearchFilter(workspace_id=wid), k=1,
               candidate_k=10, prompt_only=True)

    # First arm = max(formula=10+30=40, candidate_k=10) capped at 2 = max(2, 10) = 10.
    assert index.requested[0] == 10


def test_arm_candidate_k_cap_reduces_latency_at_k_50(monkeypatch):
    """End-to-end latency check: cap=50 should be measurably faster than
    the uncapped default at k=50, on a trusted 49-fact corpus, while still
    returning the expected number of chunks.

    The 1.5x threshold is conservative; the actual speedup on the bundled
    rebench was 1.9x (201ms -> 103ms) at cap=50.  We deliberately use a
    loose bound so this test stays stable across hardware and numpy builds.
    """
    monkeypatch.delenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", raising=False)
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
    index = NumpyVectorIndex(store)
    eng_uncapped = RecallEngine(store, emb, index, IdentityReranker())
    wid = store.get_or_create_workspace("w")
    base = (
        "Project Aurora uses Postgres for durable storage. Authentication uses PASETO. "
        "The deploy pipeline runs unit and integration tests with a canary release."
    )
    # Use 300 memories so both arms are clamped to well above 250, the
    # first-page widening ceiling. The 49-fact corpus in the original
    # version clamped both k=50 and k=200 to len(ids)==49, making the
    # two timed paths operationally identical; the 1.5x speedup
    # assertion was therefore measuring noise/cache order.
    for i in range(300):
        _add(store, emb, wid, None, f"{base} fact_index={i} workstream={i % 5}")
    flt = SearchFilter(workspace_id=wid)
    query = "What storage and auth systems does Project Aurora use?"

    def mean_ms(eng):
        # Two warmups then 11 timed samples to smooth GC and embedder warmup.
        for _ in range(2):
            eng.recall(query, flt, k=50, candidate_k=50, prompt_only=True)
        samples = []
        for _ in range(11):
            t0 = time.perf_counter()
            eng.recall(query, flt, k=50, candidate_k=50, prompt_only=True)
            samples.append((time.perf_counter() - t0) * 1000.0)
        samples.sort()
        return sum(samples[2:-2]) / 7.0  # trimmed mean, drop 2 best and 2 worst

    uncapped_ms = mean_ms(eng_uncapped)

    # Capped engine on a fresh store; rebuilding the corpus keeps the latencies
    # independent so the embedder cache state of the uncapped run cannot bias
    # the timed mean.
    monkeypatch.setenv("ENGRAPHIS_RECALL_ARM_CANDIDATE_K", "50")
    store2 = Store(":memory:")
    emb2 = _SemanticTestEmbedder(256)
    eng_capped = RecallEngine(store2, emb2, NumpyVectorIndex(store2),
                              IdentityReranker())
    wid2 = store2.get_or_create_workspace("w")
    for i in range(300):
        _add(store2, emb2, wid2, None, f"{base} fact_index={i} workstream={i % 5}")
    flt2 = SearchFilter(workspace_id=wid2)
    capped_ms = mean_ms(eng_capped)

    # Sanity: the uncapped recall returns the full k=50 trusted chunks.
    uncapped_result = eng_uncapped.recall(query, flt, k=50, candidate_k=50,
                                          prompt_only=True)
    capped_result = eng_capped.recall(query, flt2, k=50, candidate_k=50,
                                      prompt_only=True)
    assert uncapped_result.candidate_k_used == 200
    assert capped_result.candidate_k_used == 50
    # Recall quality must not regress on a trusted-only corpus.
    assert capped_result.count == uncapped_result.count
    # And latency must drop by at least 1.5x.
    assert capped_ms < uncapped_ms / 1.5, (
        f"cap=50 did not yield the expected speedup: uncapped={uncapped_ms:.1f}ms "
        f"capped={capped_ms:.1f}ms"
    )
