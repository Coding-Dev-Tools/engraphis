"""Grounded recall: cited answers, or an explicit abstain (core.grounded).

Covers the deterministic offline path (extractive answer + support-gated abstain), the
citation filter (cite only sources that individually clear the floor), the optional LLM
synthesis path (used, abstained, and failure→fallback), the memory-poisoning fencing of
the synthesis prompt, and the service-layer validation/JSON shape.
"""
import pytest

from engraphis.core.engine import MemoryEngine
from engraphis.core.grounded import ABSTAIN_SENTINEL, GROUNDED_SUPPORT_FLOOR
from engraphis.service import MemoryService, ValidationError

FACTS = [
    ("We standardised on PASETO tokens for auth, replacing JWT.", "auth"),
    ("The default package manager for frontend repos is pnpm.", "pkg"),
    ("Rate limiting is 100 requests per minute per API key.", "rate"),
]


def _engine_with_facts():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    for text, title in FACTS:
        eng.remember(text, workspace_id=wid, repo_id=rid, title=title)
    return eng, wid, rid


# ── deterministic offline path ──────────────────────────────────────────────────

def test_grounded_answers_on_topic_query():
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("which auth scheme did we standardise on?",
                              workspace_id=wid, repo_id=rid)
    assert ans.grounded and not ans.abstained
    assert ans.citations
    assert "paseto" in ans.answer.lower()          # answer drawn from a cited memory
    assert ans.synthesized is False                # extractive by default (no LLM)
    for c in ans.citations:
        assert {"n", "id", "content", "support", "provenance"} <= set(c)
        assert c["support"] >= GROUNDED_SUPPORT_FLOOR


def test_grounded_abstains_off_topic():
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("how do I bake sourdough bread?", workspace_id=wid, repo_id=rid)
    assert not ans.grounded and ans.abstained
    assert ans.answer == ""
    assert ans.support < GROUNDED_SUPPORT_FLOOR
    assert ans.reason


def test_grounded_abstains_on_empty_store():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    ans = eng.grounded_recall("anything at all", workspace_id=wid)
    assert not ans.grounded and ans.answer == "" and ans.citations == []


def test_grounded_cites_only_supporting_sources():
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("which auth scheme did we standardise on?",
                              workspace_id=wid, repo_id=rid)
    joined = " ".join(c["content"].lower() for c in ans.citations)
    assert "paseto" in joined
    assert "pnpm" not in joined                    # the unrelated memory is not cited


def test_min_support_override_forces_abstain():
    eng, wid, rid = _engine_with_facts()
    strict = eng.grounded_recall("which auth scheme did we standardise on?",
                                 workspace_id=wid, repo_id=rid, min_support=0.99)
    assert not strict.grounded and strict.abstained


# ── optional LLM synthesis path (injected fake; core stays offline by default) ───

def test_service_rejects_non_finite_support_floor():
    from engraphis.service import MemoryService, ValidationError

    service = MemoryService.create(":memory:")
    with pytest.raises(ValidationError, match="finite"):
        service.grounded_recall(
            "query", workspace="w", min_support=float("nan")
        )


class _FakeLLM:
    def __init__(self, reply, record=None):
        self._reply = reply
        self._record = record

    def complete(self, messages, **kw):
        if self._record is not None:
            self._record.append(messages)
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply

    def extract_json(self, prompt, schema):
        return {}


def test_llm_synthesis_used_when_provided():
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=wid, repo_id=rid,
                              llm=_FakeLLM("PASETO, per source [1]."))
    assert ans.grounded and ans.synthesized is True
    assert ans.answer == "PASETO, per source [1]."


def test_llm_abstain_sentinel_respected():
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=wid, repo_id=rid,
                              llm=_FakeLLM(ABSTAIN_SENTINEL))
    assert not ans.grounded and ans.abstained


def test_llm_failure_degrades_to_extractive():
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=wid, repo_id=rid,
                              llm=_FakeLLM(RuntimeError("model down")))
    assert ans.grounded and ans.synthesized is False   # fell back, did not raise
    assert "paseto" in ans.answer.lower()


def test_synthesis_prompt_fences_sources_as_data():
    # memory-poisoning defence: the system prompt must instruct the model to ignore
    # instructions inside sources, and source text must live in the user turn.
    eng, wid, rid = _engine_with_facts()
    captured = []
    eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=wid, repo_id=rid,
                        llm=_FakeLLM("ok [1]", record=captured))
    system_msg = captured[0][0]["content"].lower()
    user_msg = captured[0][1]["content"]
    assert "ignore" in system_msg and "insufficient_evidence" in system_msg
    assert "SOURCES:" in user_msg


# ── service-layer wiring (validation + JSON shape) ───────────────────────────────

def test_service_grounded_recall_shape():
    svc = MemoryService.create(":memory:")
    svc.remember("We use PASETO for auth.", workspace="acme", repo="backend", title="auth")
    out = svc.grounded_recall("which auth scheme did we standardise on?", workspace="acme", repo="backend")
    assert {"query", "grounded", "abstained", "answer", "support", "citations"} <= set(out)


def test_service_grounded_recall_unknown_workspace_is_soft():
    svc = MemoryService.create(":memory:")
    out = svc.grounded_recall("anything", workspace="ghost")
    assert out["grounded"] is False and "ghost" in out["reason"]


def test_service_grounded_recall_validates_query():
    svc = MemoryService.create(":memory:")
    with pytest.raises(ValidationError):
        svc.grounded_recall("   ", workspace="acme")


def test_grounded_eval_fixture_scores_perfectly():
    # Locks the abstain gate into the CI gate: the eval fixture must fully separate
    # answerable from off-topic queries (see eval/grounded.py). A regression in the
    # support signal or the floor trips this, not just the standalone eval.
    from eval.grounded import run
    r = run()
    assert r["answer_rate"] == 1.0 and r["abstain_rate"] == 1.0


# ── interaction reinforcement: reward only what was actually used ────────────────

def test_grounded_reinforces_only_cited_sources():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    auth = eng.remember("We standardised on PASETO tokens for auth, replacing JWT.",
                        workspace_id=wid, repo_id=rid, title="auth")
    pkg = eng.remember("The default package manager for frontend repos is pnpm.",
                       workspace_id=wid, repo_id=rid, title="pkg")
    a0 = eng.store.get_memory(auth).access_count
    p0 = eng.store.get_memory(pkg).access_count
    ans = eng.grounded_recall("which auth scheme did we standardise on?",
                              workspace_id=wid, repo_id=rid)
    assert ans.grounded
    cited = {c["id"] for c in ans.citations}
    assert auth in cited and pkg not in cited
    assert eng.store.get_memory(auth).access_count > a0     # cited → reinforced
    assert eng.store.get_memory(pkg).access_count == p0     # uncited → untouched


def test_abstain_reinforces_nothing():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    ids = [eng.remember(t, workspace_id=wid, repo_id=rid, title=str(i))
           for i, t in enumerate(["We standardised on PASETO tokens for auth.",
                                  "The default package manager is pnpm.",
                                  "Rate limiting is 100 requests per minute per key."])]
    before = {i: eng.store.get_memory(i).access_count for i in ids}
    ans = eng.grounded_recall("how do I bake sourdough bread?", workspace_id=wid, repo_id=rid)
    assert ans.abstained and not ans.grounded
    after = {i: eng.store.get_memory(i).access_count for i in ids}
    assert before == after                                   # abstain rewards nothing


def test_citations_capped_and_contiguously_numbered():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    facts = ["Auth uses PASETO tokens for user authentication.",
             "Service-to-service authentication uses mTLS certificates.",
             "Admin authentication additionally requires a hardware key.",
             "Authentication sessions expire after 30 minutes idle."]
    for i, t in enumerate(facts):
        eng.remember(t, workspace_id=wid, repo_id=rid, title=f"auth{i}", resolve_conflicts=False)
    ans = eng.grounded_recall("what is the authentication scheme?", workspace_id=wid,
                              repo_id=rid, max_citations=2)
    assert ans.grounded
    assert len(ans.citations) <= 2                           # cap holds
    assert [c["n"] for c in ans.citations] == list(range(1, len(ans.citations) + 1))


# ── review-driven regression guards (B1 support/citation invariant, B2, S1, S2) ──

def test_reported_support_belongs_to_a_cited_source():
    # Invariant: the headline `support` is always the support of citation [1], and is the
    # max over cited sources — we never advertise evidence we don't actually show (B1).
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    facts = ["We standardised on PASETO tokens for auth, replacing JWT.",
             "The default package manager for frontend repos is pnpm.",
             "Rate limiting is 100 requests per minute per API key.",
             "Database migrations run via alembic on deploy.",
             "Application secrets are stored in Vault, never in the repo.",
             "The staging environment redeploys on every merge to main."]
    for i, t in enumerate(facts):
        eng.remember(t, workspace_id=wid, repo_id=rid, title=str(i), resolve_conflicts=False)
    for q in ["which auth scheme did we standardise on?",
              "where are application secrets stored?",
              "what is the API rate limit per key?"]:
        ans = eng.grounded_recall(q, workspace_id=wid, repo_id=rid, max_citations=2)
        assert ans.grounded and ans.citations
        top = max(c["support"] for c in ans.citations)
        assert abs(ans.support - top) < 1e-4                 # headline == best cited
        assert ans.citations[0]["support"] == top            # strongest evidence first


def test_llm_prose_without_citation_falls_back_to_extractive():
    # B2: prose that cites nothing may be fabricated → must NOT be accepted as grounded
    # prose; fall back to the deterministic, cited extractive answer.
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=wid,
                              repo_id=rid, llm=_FakeLLM("It is Kerberos, with no markers."))
    assert ans.grounded and ans.synthesized is False
    assert "paseto" in ans.answer.lower()                    # the real, cited evidence
    assert "kerberos" not in ans.answer.lower()              # uncited prose rejected


def test_llm_abstain_sentinel_has_no_citations():
    # S1: an abstain (either path) carries no citations, for contract parity.
    eng, wid, rid = _engine_with_facts()
    ans = eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=wid,
                              repo_id=rid, llm=_FakeLLM(ABSTAIN_SENTINEL))
    assert ans.abstained and not ans.grounded
    assert ans.citations == [] and ans.answer == ""


def test_grounded_recall_scope_isolation_by_workspace():
    # S2: a memory in workspace A is never surfaced/cited when querying workspace B.
    eng = MemoryEngine.create(":memory:")
    a = eng.store.get_or_create_workspace("acme")
    b = eng.store.get_or_create_workspace("beta")
    eng.remember("We standardised on PASETO tokens for auth.", workspace_id=a, title="auth")
    ans = eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=b)
    assert not ans.grounded and ans.citations == []


def test_grounded_recall_respects_mtypes_filter():
    # S2: restricting to a memory type the fact isn't → nothing supports → abstain.
    from engraphis.core.interfaces import MemoryType
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("We standardised on PASETO tokens for auth.", workspace_id=wid, repo_id=rid,
                 mtype=MemoryType.SEMANTIC, title="auth")
    ans = eng.grounded_recall("which auth scheme did we standardise on?", workspace_id=wid,
                              repo_id=rid, mtypes=[MemoryType.PROCEDURAL])
    assert not ans.grounded
