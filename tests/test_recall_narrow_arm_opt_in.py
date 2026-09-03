"""Tests for the opt-in ``ENGRAPHIS_RECALL_NARROW_ARM`` latency knob.

B2's 4th-pass review found the PR #171 prompt-only widening
(``candidate_k + min(250, candidate_k*3)``) costs ~5x more matrix-vector
work on a 49-fact corpus at k=8 (32ms -> 175ms).  That regression was the
user-accepted trade-off for keeping recall quality on small-k callers,
but operators who care more about latency than top-of-list precision can
opt in to a narrower arm by setting ``ENGRAPHIS_RECALL_NARROW_ARM=1``.

When the knob is enabled, the first prompt-only arm is clamped to
``max(k, min(50, candidate_k*2))`` and the escalation ceiling is clamped
to ``min(PROMPT_ONLY_MAX_CANDIDATES, candidate_k*4)`` so the second page
does not silently undo the savings.  The narrow arm is gated on k <= 20
because larger-k callers still need the full widening to find approved
evidence.

Default behavior (env var unset / 0 / empty) is unchanged — the wider
arm from PR #171 is preserved verbatim.

These tests prove both halves of the contract: the env var is honored
when set, and the wider arm is the default when it is not.
"""
from __future__ import annotations

from engraphis.backends import DeterministicEmbedder
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.interfaces import MemoryRecord, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store


class _SemanticTestEmbedder(DeterministicEmbedder):
    supports_semantic_search = True
    embedding_mode = "semantic"


class _RecordingIndex:
    """Vector-index double that records every arm size it was queried with."""

    def __init__(self):
        self.requested: list[int] = []

    def search(self, query, k, *, filter=None):
        self.requested.append(int(k))
        return [(f"mem_{i}", float(k - i)) for i in range(min(k, 4))]


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


def test_narrow_arm_opt_in_default_is_false(monkeypatch):
    """Without the env var the opt-in flag is False — the wider arm stays the
    default for every caller, including small-k ones."""
    monkeypatch.delenv("ENGRAPHIS_RECALL_NARROW_ARM", raising=False)
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._narrow_arm_opt_in is False


def test_narrow_arm_opt_in_reads_env_var(monkeypatch):
    """Any non-empty, non-zero env value flips the opt-in flag; bad/zero
    values are ignored so a typo cannot silently change retrieval behavior."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "1")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._narrow_arm_opt_in is True

    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "true")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._narrow_arm_opt_in is True

    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "0")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._narrow_arm_opt_in is False

    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "false")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._narrow_arm_opt_in is False

    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "   ")
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       _RecordingIndex(), IdentityReranker())
    assert eng._narrow_arm_opt_in is False


def test_narrow_arm_opt_in_changes_first_arm_at_k_8(monkeypatch):
    """With the env var set, the first prompt-only arm at k=8 shrinks from
    8 + min(250, 24) = 32 to min(50, 8*2) = 16.  This is the latency win
    the knob is meant to unlock."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "1")
    index = _RecordingIndex()
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       index, IdentityReranker())
    store = eng.store
    wid = store.get_or_create_workspace("w")
    for i in range(60):
        _add(store, eng.embedder, wid, None, f"fact {i}")

    result = eng.recall("fact 5", SearchFilter(workspace_id=wid), k=8,
                        candidate_k=8, prompt_only=True)

    assert index.requested[0] == 16  # 8 + 24 (default) would have been 32
    assert result.candidate_k_used == 16
    # The result must still be non-empty: the narrow arm must not crash
    # recall on a trusted-only corpus.
    assert result.count >= 1


def test_narrow_arm_does_not_change_default(monkeypatch):
    """Without the env var the wider arm is preserved.  This is the
    user-accepted trade-off: 5x latency for top-of-list recall."""
    monkeypatch.delenv("ENGRAPHIS_RECALL_NARROW_ARM", raising=False)
    index = _RecordingIndex()
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       index, IdentityReranker())
    store = eng.store
    wid = store.get_or_create_workspace("w")
    for i in range(60):
        _add(store, eng.embedder, wid, None, f"fact {i}")

    result = eng.recall("fact 5", SearchFilter(workspace_id=wid), k=8,
                        candidate_k=8, prompt_only=True)

    # PR #171 default: 8 + min(250, 8*3) = 8 + 24 = 32
    assert index.requested[0] == 32
    assert result.candidate_k_used == 32


def test_narrow_arm_only_applies_to_small_k(monkeypatch):
    """The narrow arm is gated on k <= 20.  A k=50 caller must still see
    the wider arm even with the env var set — that is the caller class
    that motivated the widening in the first place."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "1")
    index = _RecordingIndex()
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       index, IdentityReranker())
    store = eng.store
    wid = store.get_or_create_workspace("w")
    for i in range(60):
        _add(store, eng.embedder, wid, None, f"fact {i}")

    eng.recall("fact 5", SearchFilter(workspace_id=wid), k=50,
               candidate_k=50, prompt_only=True)

    # k=50 is above the gate; wider arm preserved: 50 + min(250, 150) = 200.
    assert index.requested[0] == 200

    # k=20 is at the boundary and should still be narrow: 20*2 = 40.
    index2 = _RecordingIndex()
    eng2 = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                        index2, IdentityReranker())
    eng2.recall("fact 5", SearchFilter(workspace_id=wid), k=20,
                candidate_k=20, prompt_only=True)
    assert index2.requested[0] == 40

    # k=21 is just past the gate; wider arm preserved: 21 + min(250, 63) = 84.
    index3 = _RecordingIndex()
    eng3 = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                        index3, IdentityReranker())
    eng3.recall("fact 5", SearchFilter(workspace_id=wid), k=21,
                candidate_k=21, prompt_only=True)
    assert index3.requested[0] == 84


def test_narrow_arm_does_not_apply_to_non_prompt_only(monkeypatch):
    """The narrow arm is a prompt-only knob.  A non-prompt recall with the
    env var set must behave exactly like the default, because the wider
    widening lives inside the ``if prompt_only`` block."""
    monkeypatch.setenv("ENGRAPHIS_RECALL_NARROW_ARM", "1")
    index = _RecordingIndex()
    eng = RecallEngine(Store(":memory:"), _SemanticTestEmbedder(256),
                       index, IdentityReranker())
    store = eng.store
    wid = store.get_or_create_workspace("w")
    for i in range(60):
        _add(store, eng.embedder, wid, None, f"fact {i}")

    # include_untrusted=True forces prompt_only=False regardless of the
    # env var; the first arm must be the raw candidate_k with no widening.
    eng.recall("fact 5", SearchFilter(workspace_id=wid), k=8,
               candidate_k=8, prompt_only=False, include_untrusted=True)
    assert index.requested[0] == 8
