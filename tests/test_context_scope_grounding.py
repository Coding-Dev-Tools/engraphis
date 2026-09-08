"""Public recall preserves ownership and complete cited operational claims."""

import pytest

from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import Candidate, MemoryRecord, Scope


class _Reply:
    def __init__(self, text):
        self.text = text
        self.messages = []

    def complete(self, messages, **kwargs):
        self.messages = messages
        return self.text


@pytest.fixture
def engine():
    instance = MemoryEngine.create(":memory:", auto_evolve=False)
    yield instance
    instance.store.close()


def _workspace(engine):
    return engine.store.get_or_create_workspace("evidence-review")


@pytest.mark.parametrize("owners", [("Ada", "Ben"), ("Ada", "Ada")])
def test_workspace_recall_keeps_repo_claims_and_ownership_after_restart(tmp_path, owners):
    database = str(tmp_path / "scoped-evidence.db")
    instance = MemoryEngine.create(database, auto_evolve=False)
    workspace = _workspace(instance)
    repos = [instance.store.get_or_create_repo(workspace, name) for name in ("front", "back")]
    ids = []
    for repo, owner in zip(repos, owners):
        result = instance.remember_with_resolution(
            f"The API owner is {owner}.", workspace_id=workspace, repo_id=repo,
            scope=Scope.REPO, subject_key="api", claim_kind="owner",
        )
        assert result["op"] == "add"
        ids.append(result["id"])
    instance.store.close()

    instance = MemoryEngine.create(database, auto_evolve=False)
    try:
        result = instance.recall("API owner", workspace_id=workspace, token_budget=1500,
                                 reinforce=False)
        assert {chunk.id for chunk in result.packed_chunks} == set(ids)
        assert result.usage.omitted_count == 0
        for owner in owners:
            assert f"The API owner is {owner}." in result.context
        for repo, chunk in zip(repos, ids):
            packed = next(item for item in result.packed_chunks if item.id == chunk)
            assert f"repo_id={repo}" in packed.attribution
            assert f"workspace_id={workspace}" in packed.attribution
            assert packed.attribution in result.context

        answer = instance.grounded_recall("API owner", workspace_id=workspace,
                                           token_budget=1500, reinforce=False)
        assert {citation["id"] for citation in answer.citations} == set(ids)
        for repo in repos:
            assert f"repo_id={repo}" in answer.answer
        assert answer.to_dict()["answer_coverage"] == "unknown"

        model = _Reply("The API owner is Ada [1].")
        shortened = instance.grounded_recall(
            "API owner", workspace_id=workspace, token_budget=1500, llm=model,
            reinforce=False,
        )
        assert shortened.grounded and not shortened.synthesized
        for repo in repos:
            assert f"repo_id={repo}" in shortened.answer
            assert f"repo_id={repo}" in model.messages[1]["content"]
    finally:
        instance.store.close()


@pytest.mark.parametrize("metadata", [
    {"subject_key": "api", "claim_kind": "owner"},
    {"claim_key": "api-owner"},
    {"consolidation_family": "api-owner"},
    {"source_ids": ["mem_first", "mem_second"]},
    {"supersedes": ["mem_first"]},
])
def test_lineage_is_not_proof_that_distinct_records_are_redundant(metadata):
    candidates = [
        Candidate("mem_first", 1.0, record=MemoryRecord(
            "mem_first", "The API owner is Ada.", metadata=metadata,
            workspace_id="ws_a", repo_id="repo_a", valid_from=100, valid_to=200,
        )),
        Candidate("mem_second", 0.95, record=MemoryRecord(
            "mem_second", "The API owner is Ben.", metadata=metadata,
            workspace_id="ws_a", repo_id="repo_a", valid_from=200,
        )),
    ]
    packed = DeterministicContextPacker().pack("API owner history", candidates, 1500)
    assert {chunk.id for chunk in packed.chunks} == {"mem_first", "mem_second"}
    assert packed.usage.omitted_count == 0


def test_repeated_canonical_candidate_is_still_packed_once():
    record = MemoryRecord("mem_shared", "The API owner is Ada.")
    result = DeterministicContextPacker().pack("API owner", [
        Candidate(record.id, 0.7, arm="lexical", record=record),
        Candidate(record.id, 0.9, arm="graph", record=record),
    ], 100)
    assert [chunk.id for chunk in result.chunks] == [record.id]
    assert result.usage.omitted_count == 1


@pytest.mark.parametrize("scope,field", [
    (Scope.WORKSPACE, "workspace_id"),
    (Scope.REPO, "repo_id"),
    (Scope.SESSION, "session_id"),
    (Scope.USER, "workspace_id"),
])
def test_mixed_owner_labels_survive_packing_with_identical_bodies(scope, field):
    candidates = []
    for suffix in ("left", "right"):
        record = MemoryRecord(
            f"mem_{suffix}", "The API owner is Ada.", scope=scope,
            workspace_id="ws_shared", repo_id="repo_shared", session_id="ses_shared",
            subject_key="api", claim_kind="owner",
        )
        setattr(record, field, suffix)
        candidates.append(Candidate(record.id, 1.0, record=record))
    packed = DeterministicContextPacker().pack("API owner", candidates, 1500)
    assert packed.usage.packed_count == 2
    assert f"{field}=left" in packed.context
    assert f"{field}=right" in packed.context
    assert packed.usage.context_tokens == RegexTokenCounter()(packed.context)


def test_session_claim_does_not_hide_repository_ancestor(engine):
    workspace = _workspace(engine)
    repo = engine.store.get_or_create_repo(workspace, "service")
    session = engine.start_session(workspace, repo, agent="test")
    ancestor = engine.remember(
        "The API owner is Ada.", workspace_id=workspace, repo_id=repo,
        scope=Scope.REPO, subject_key="api", claim_kind="owner",
    )
    current = engine.remember(
        "The API owner is Ben.", workspace_id=workspace, repo_id=repo,
        session_id=session, scope=Scope.SESSION, subject_key="api", claim_kind="owner",
    )
    result = engine.recall("API owner", workspace_id=workspace, repo_id=repo,
                           session_id=session, reinforce=False)
    assert {chunk.id for chunk in result.packed_chunks} == {ancestor, current}
    assert "scope=repo" in result.context and "scope=session" in result.context
    assert f"session_id={session}" in result.context


def test_canonical_temporal_reads_select_revisions_without_family_collapse(engine):
    workspace = _workspace(engine)
    repo = engine.store.get_or_create_repo(workspace, "service")
    old = engine.remember(
        "The API owner is Ada.", workspace_id=workspace, repo_id=repo,
        subject_key="api", claim_kind="owner", valid_from=100,
    )
    known_before = engine.store.get_memory(old).ingested_at
    corrected = engine.correct(old, "The API owner is Clara.")
    current = engine.recall("API owner", workspace_id=workspace, repo_id=repo,
                            reinforce=False)
    historical = engine.recall(
        "API owner", workspace_id=workspace, repo_id=repo,
        valid_at=101, known_at=known_before, reinforce=False,
    )
    assert [chunk.id for chunk in current.packed_chunks] == [corrected["id"]]
    assert [chunk.id for chunk in historical.packed_chunks] == [old]
    assert "Clara" in current.context and "Ada" not in current.context
    assert "Ada" in historical.context and "Clara" not in historical.context


@pytest.mark.parametrize("source,shortened", [
    ("Deployments require approval unless the incident is an emergency.",
     "Deployments require approval [1]."),
    ("Deployment is permitted only after the backup succeeds.",
     "Deployment is permitted [1]."),
    ("Deployment is prohibited until Friday.", "Deployment is prohibited [1]."),
    ("Deployments are allowed. Except during incident response.",
     "Deployments are allowed [1]."),
    ("Deployment authorization belongs to ServiceAlpha in staging.",
     "Deployment authorization belongs to ServiceAlpha [1]."),
    ("The deployment timeout is 30 seconds.", "The deployment timeout is 30 [1]."),
    ("The deployment offset is -0.5.", "The deployment offset is 0.5 [1]."),
    ("Deployment targets ServiceAlpha.", "Deployment targets servicealpha [1]."),
    ("Le déploiement est autorisé sauf en production.",
     "Le déploiement est autorisé [1]."),
    ("El despliegue está permitido salvo en producción.",
     "El despliegue está permitido [1]."),
    ("允许部署到测试环境，除非操作员拒绝此次发布。", "允许部署到测试环境 [1]。"),
    ("Provided the backup succeeds, deployment is permitted.",
     "the backup succeeds, deployment is permitted [1]."),
])
def test_synthesis_cannot_shorten_or_change_complete_evidence(engine, source, shortened):
    workspace = _workspace(engine)
    engine.remember(source, workspace_id=workspace)
    answer = engine.grounded_recall(
        source, workspace_id=workspace, llm=_Reply(shortened), min_support=0,
        token_budget=1500, reinforce=False,
    )
    assert answer.grounded and not answer.synthesized
    assert answer.answer != shortened
    assert source in answer.answer
    assert answer.to_dict()["answer_coverage"] == "unknown"


@pytest.mark.parametrize("style", ["before", "after", "inside_punctuation"])
def test_synthesis_accepts_complete_multisentence_units(engine, style):
    source = "Deployments are allowed. Except during incident response."
    workspace = _workspace(engine)
    engine.remember(source, workspace_id=workspace)
    replies = {
        "before": f"[1] {source}",
        "after": f"{source} [1]",
        "inside_punctuation": f"{source[:-1]} [1].",
    }
    answer = engine.grounded_recall(source, workspace_id=workspace,
                                    llm=_Reply(replies[style]), reinforce=False)
    assert answer.grounded and answer.synthesized
    assert answer.answer == replies[style]


def test_synthesis_and_extractive_fallback_preserve_complete_title(engine):
    title = "Deployment approval " + "for customer services " * 8 + "except production"
    content = "Operator approval is required."
    workspace = _workspace(engine)
    engine.remember(content, title=title, workspace_id=workspace)
    model = _Reply(f"{content} [1]")
    answer = engine.grounded_recall(content, workspace_id=workspace, llm=model,
                                    token_budget=1500, reinforce=False)
    assert answer.grounded and not answer.synthesized
    assert title in answer.answer
    assert title in model.messages[1]["content"]
    recalled = engine.recall(content, workspace_id=workspace, token_budget=1500,
                             reinforce=False)
    assert title in recalled.context

    too_small = engine.recall(content, workspace_id=workspace, token_budget=20,
                              reinforce=False)
    assert too_small.context == "" and not too_small.packed_chunks


def test_title_case_difference_cannot_remove_its_subject_binding():
    record = MemoryRecord("mem_title", "servicealpha is available.", title="ServiceAlpha")
    result = DeterministicContextPacker().pack("availability", [
        Candidate(record.id, 1.0, record=record),
    ], 100)
    assert "ServiceAlpha" in result.context
    assert "servicealpha" in result.context
    assert result.usage.context_tokens == RegexTokenCounter()(result.context)


def test_partial_question_reports_unknown_coverage(engine):
    workspace = _workspace(engine)
    engine.remember("Production deployment approval comes from the release manager.",
                    workspace_id=workspace)
    answer = engine.grounded_recall(
        "Who gives production deployment approval and what are the rollback steps?",
        workspace_id=workspace, reinforce=False,
    )
    assert answer.grounded
    assert answer.to_dict()["answer_coverage"] == "unknown"
