"""Adversarial claim changes must survive context compression."""
import pytest

from engraphis.core.context import DeterministicContextPacker
from engraphis.core.interfaces import Candidate, MemoryRecord


def _candidate(memory_id, content, *, score=1.0, summary=""):
    return Candidate(
        id=memory_id, score=score, arm="lexical",
        record=MemoryRecord(id=memory_id, content=content, summary=summary),
    )


@pytest.mark.parametrize("left,right", [
    ("30 days", "90 days"),
    ("30 seconds", "30 minutes"),
    ("2026-09-01", "2026-09-02"),
    ("production", "staging"),
    ("ServiceAlpha", "ServiceBeta"),
    ("ServiceAlpha", "servicealpha"),
    ("0.5", "-0.5"),
    ("10.5", "10,5"),
    ("enabled", "disabled"),
    ("not authorized", "authorized"),
    ("unless the service is unavailable", "unless the test suite passes"),
    ("except production", "except staging"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_changed_claim_is_never_removed_as_a_near_duplicate(left, right, reverse):
    template = "The operational policy requires the service to record the setting as {}."
    texts = [template.format(left), template.format(right)]
    candidates = [
        _candidate("mem_a", texts[0], score=0.95),
        _candidate("mem_b", texts[1], score=0.90),
    ]
    if reverse:
        candidates.reverse()
    result = DeterministicContextPacker().pack("operational policy setting", candidates, 500)
    assert {chunk.id for chunk in result.chunks} == {"mem_a", "mem_b"}
    assert all(text in result.context for text in texts)


@pytest.mark.parametrize("summary", [
    "Production deploys are allowed unless staging tests pass.",
    "Staging deploys are allowed unless production tests pass.",
    "Production deploys are allowed unless production tests fail.",
])
def test_summary_cannot_change_a_condition_binding(summary):
    content = "Production deploys are allowed unless production tests pass."
    result = DeterministicContextPacker().pack(
        "production deploys", [_candidate("mem_policy", content, summary=summary)], 100,
    )
    assert result.chunks[0].excerpt == content
    assert result.chunks[0].reason == "full"


def test_summary_cannot_replace_numerical_claim_with_different_value():
    result = DeterministicContextPacker().pack(
        "retention days",
        [_candidate("mem_retention", "Logs remain available for 90 days.",
                    summary="Logs remain available for 30 days.")], 100,
    )
    assert result.chunks[0].excerpt == "Logs remain available for 90 days."


@pytest.mark.parametrize("content", [
    "Production deployments are permitted unless the operator has rejected the release.",
    "Production deployments are permitted only after the operator verifies the backup.",
    "The retention duration for production audit logs is 90 days.",
    "The permitted timeout is 30 minutes for the production worker.",
])
def test_tight_budget_never_keeps_a_partial_qualified_or_numerical_claim(content):
    packer = DeterministicContextPacker()
    candidate = _candidate("mem_policy", content)
    for budget in range(1, packer.count_tokens(content) + 5):
        result = packer.pack("production policy", [candidate], budget)
        assert result.usage.context_tokens <= budget
        assert not result.chunks or result.chunks[0].excerpt == content


def test_shared_clause_and_new_following_clause_keep_their_source_binding():
    common = "Deployments require approval from the release owner."
    novel = "The backup retention duration is 90 days."
    result = DeterministicContextPacker().pack(
        "deployment backup policy",
        [_candidate("mem_a", common, score=1.5),
         _candidate("mem_b", common + " " + novel, score=0.9)],
        100,
    )
    assert result.context.count(common) == 2
    assert novel in result.context
    assert result.chunks[1].excerpt == common + " " + novel
    assert result.chunks[1].reason == "full"


@pytest.mark.parametrize("content", [
    "Le déploiement est autorisé sauf en environnement de production.",
    "允许部署到测试环境，除非操作员拒绝此次发布。",
    "El despliegue está permitido salvo en el entorno de producción.",
    "The artifact destination is the private production registry.",
    "Release authorization belongs to ServiceAlpha in the staging environment.",
])
def test_multilingual_or_subject_tail_is_never_removed_by_prefix_fitting(content):
    packer = DeterministicContextPacker(token_counter=len, token_counter_identity="test.characters")
    candidate = _candidate("mem_multilingual", content)
    for budget in (8, len(content) // 2, len(content) - 1, len(content) + 8):
        result = packer.pack("deployment", [candidate], budget)
        assert result.usage.context_tokens <= budget
        assert not result.chunks or result.chunks[0].excerpt == content


def test_semicolon_condition_remains_bound_to_its_governing_claim():
    first = "Deployments may proceed; production requires operator approval."
    second = "Deployments may proceed; staging requires operator approval."
    result = DeterministicContextPacker().pack(
        "deployment approval", [_candidate("mem_a", first), _candidate("mem_b", second)], 100,
    )
    assert [chunk.excerpt for chunk in result.chunks] == [first, second]


@pytest.mark.parametrize("reverse", [False, True])
def test_identical_bodies_retain_distinct_environment_titles_and_citations(reverse):
    content = "Logs remain available for 30 days."
    candidates = [
        _candidate("mem_production", content, score=0.95),
        _candidate("mem_staging", content, score=0.90),
    ]
    candidates[0].record.title = "Production log retention"
    candidates[1].record.title = "Staging log retention"
    if reverse:
        candidates.reverse()

    result = DeterministicContextPacker().pack(
        "production and staging log retention", candidates, 500,
    )

    assert {chunk.id for chunk in result.chunks} == {"mem_production", "mem_staging"}
    assert "[1] Production log retention\n" + content in result.context
    assert "[2] Staging log retention\n" + content in result.context


def test_repeated_clause_retains_its_own_subject_and_condition_binding():
    first = "Production uses Atlas. It retains logs for 30 days."
    second = "Staging uses Boreal. It retains logs for 30 days. Except during incident response."
    result = DeterministicContextPacker().pack(
        "production staging log retention",
        [_candidate("mem_production", first), _candidate("mem_staging", second)], 500,
    )

    assert {chunk.id: chunk.excerpt for chunk in result.chunks} == {
        "mem_production": first, "mem_staging": second,
    }


def test_independent_sources_keep_separate_citations_for_identical_evidence():
    content = "The default request timeout is 30 seconds."
    candidates = [_candidate("mem_config", content), _candidate("mem_test", content)]
    candidates[0].record.provenance = {"source": "repo/config.py"}
    candidates[1].record.provenance = {"source": "repo/tests/test_config.py"}
    result = DeterministicContextPacker().pack("request timeout evidence", candidates, 500)

    assert [chunk.id for chunk in result.chunks] == ["mem_config", "mem_test"]
    assert result.context.count(content) == 2
    assert result.usage.packed_count == 2
