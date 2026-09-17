import json
from pathlib import Path
import shutil

import pytest

from eval.coding_acceptance import validate_corpus
from eval.coding_corpus import (
    CORPUS_VERSION_V1,
    CORPUS_VERSION_V2,
    Corpus,
    DATASET_ROOT,
    RUNTIME_SCHEMA_V1,
    RUNTIME_SCHEMA_V2,
    ReaderResponse,
    build_artifacts,
    load_corpus,
    run_oracle,
    run_reader,
    scenario_workspace,
)


def test_checked_in_corpus_has_frozen_family_split_and_categories():
    corpus = load_corpus()
    assert len(corpus.scenarios()) == 400
    assert len({scenario.family_id for scenario in corpus.scenarios()}) == 40
    assert {scenario.category for scenario in corpus.scenarios()} == {
        "corrections", "temporal_history", "scope_boundaries", "paraphrases",
        "code_relationships", "unsupported_questions", "poisoning", "condition_values",
        "long_documents", "multilingual",
    }
    assert {split: len(corpus.scenarios(split=split)) for split in (
        "development", "validation", "held_out",
    )} == {"development": 80, "validation": 80, "held_out": 240}
    assert all(scenario.split == "held_out" for scenario in corpus.scenarios(split="held_out"))
    families = corpus.runtime["families"]
    assert len(corpus.runtime["template_groups"]) == 10
    assert {group: sum(row["template_group"] == group for row in families)
            for group in corpus.runtime["template_groups"]} == {group: 4 for group in corpus.runtime["template_groups"]}



def test_v1_loader_contract_remains_available_for_historical_pilot():
    corpus = load_corpus()
    assert corpus.runtime["schema"] == RUNTIME_SCHEMA_V1
    assert corpus.runtime["version"] == CORPUS_VERSION_V1


def test_v2_development_fixture_uses_structural_long_document_contract(tmp_path):
    root = tmp_path / "coding_memory_v2"
    generated = build_artifacts(
        root,
        version=CORPUS_VERSION_V2,
        family_ids=("atlas-north",),
    )
    assert generated["runtime"]["schema"] == RUNTIME_SCHEMA_V2
    assert generated["runtime"]["version"] == CORPUS_VERSION_V2
    assert len(generated["runtime"]["scenarios"]) == 10
    assert {row["split"] for row in generated["manifest"]["scenarios"]} == {"development"}

    corpus = Corpus(root, generated["manifest"], generated["runtime"])
    scenario = corpus.get("atlas-north:long_documents")
    context_text = "\n".join(item.content for item in corpus.context(scenario))
    for value in ("vault", "14", "UTC"):
        assert value in context_text
    for key in ("store", "retention_days", "timezone"):
        assert key in scenario.task.prompt
        assert key in scenario.task.expected_change

    workspace = tmp_path / "repository"
    with scenario_workspace(scenario, workspace) as prepared:
        initial = run_oracle(scenario, workspace=prepared)
        source = json.loads(scenario.source_path.read_text(encoding="utf-8"))
        signature = source["family_signature"]
        contract = repr({
            "store": signature["store"],
            "retention_days": signature["retention_days"],
            "timezone": signature["timezone"],
        })
        service = prepared / "service.py"
        text = service.read_text(encoding="utf-8")
        changed_text = text.replace(
            'return sections.get("overview") if name == "late-constraint" else sections.get(name)',
            f'return {contract} if name == "late-constraint" else sections.get(name)',
            1,
        )
        assert changed_text != text
        with service.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(changed_text)
        repaired = run_oracle(scenario, workspace=prepared)
    assert initial.passed is False
    assert repaired.passed is True
    oracle_text = scenario.oracle_path.read_text(encoding="utf-8")
    assert "find_section('late-constraint') == {'store':" in oracle_text
    assert "retains" not in oracle_text


def test_v2_build_refuses_to_overwrite_existing_output(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    marker = root / "marker.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to regenerate"):
        build_artifacts(root, version=CORPUS_VERSION_V2, family_ids=("atlas-north",))
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_loader_fails_closed_without_explicit_build(tmp_path):
    with pytest.raises(ValueError, match="explicit --materialize"):
        load_corpus(tmp_path / "missing")


def test_implementation_origin_stays_separate_from_independent_gate():
    manifest_path = DATASET_ROOT / "manifest.json"
    attestation_path = DATASET_ROOT / "attestation.txt"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = validate_corpus(manifest, require_independent=False, attestation_path=attestation_path)
    assert result["origin"] == "implementation_team"
    assert result["independently_authored_verified"] is False
    with pytest.raises(ValueError, match="not independent"):
        validate_corpus(manifest, attestation_path=attestation_path)


@pytest.mark.parametrize("relative", [
    Path("families/atlas-green.json"),
    Path("oracles/atlas-green--corrections.py"),
])
def test_referenced_source_and_oracle_bytes_are_digest_bound(tmp_path, relative):
    copied = tmp_path / "coding_memory_v1"
    shutil.copytree(DATASET_ROOT, copied)
    path = copied / relative
    path.write_bytes(path.read_bytes() + b"\n# tampered\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_corpus(copied)


def test_replay_applies_correction_time_and_scope_rules():
    corpus = load_corpus()
    corrections = next(item for item in corpus.scenarios() if item.category == "corrections")
    ledger = corpus.replay(corrections)
    context = corpus.context(corrections)
    assert [event["op"] for event in ledger.events] == ["remember", "correct", "remember"]
    assert [item.id for item in context] == [corrections.task.required_evidence_ids[0]]
    assert ledger._records[f"history:{corrections.family_id}:timeout"].valid_to == 20.0

    temporal = next(item for item in corpus.scenarios() if item.category == "temporal_history")
    temporal_ids = {item.id for item in corpus.context(temporal)}
    assert temporal_ids == set(temporal.task.required_evidence_ids)

    scoped = next(item for item in corpus.scenarios() if item.category == "scope_boundaries")
    scoped_ids = {item.id for item in corpus.context(scoped)}
    assert f"workspace:{scoped.family_id}:scope" in scoped_ids
    assert f"session:{scoped.family_id}:scope" not in scoped_ids
    assert f"forbidden:{scoped.family_id}:sibling" not in scoped_ids


def test_materialized_repository_does_not_disclose_target_memory_values():
    corpus = load_corpus()
    for family_id in sorted({scenario.family_id for scenario in corpus.scenarios()}):
        scenario = corpus.get(f"{family_id}:corrections")
        source = json.loads(scenario.source_path.read_text(encoding="utf-8"))
        signature = source["family_signature"]
        target_values = (
            str(signature["new_timeout"]),
            signature["new_policy"],
            signature["new_helper"],
            str(signature["new_limit"]),
            f"repo-{family_id}",
            str(signature["retention_days"]),
            signature["timezone"],
            signature["new_locale"],
        )
        with scenario_workspace(scenario) as workspace:
            materialized = b"\n".join(path.read_bytes() for path in workspace.rglob("*") if path.is_file())
        for value in target_values:
            assert value.encode("utf-8") not in materialized, (family_id, value)


def test_oracle_fails_on_fixture_and_passes_after_real_source_change(tmp_path):
    corpus = load_corpus()
    scenario = next(item for item in corpus.scenarios() if item.category == "corrections")
    initial = run_oracle(scenario)
    assert initial.passed is False
    assert initial.returncode != 0

    workspace = tmp_path / "repository"
    with scenario_workspace(scenario, workspace) as prepared:
        source = json.loads(scenario.source_path.read_text(encoding="utf-8"))
        signature = source["family_signature"]
        service = prepared / "service.py"
        text = service.read_text(encoding="utf-8")
        text = text.replace(
            f"return {signature['old_timeout']!r}",
            f"return {signature['new_timeout']!r}",
            1,
        )
        with service.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        changed = run_oracle(scenario, workspace=prepared)
    assert changed.passed is True


def test_injectable_reader_scores_answer_abstention_and_poisoning_separately():
    corpus = load_corpus()
    answerable = next(item for item in corpus.scenarios() if item.category == "corrections")

    def good_reader(request):
        return ReaderResponse(
            answer="The current timeout is " + " ".join(request.scenario.task.answer_tokens) + ".",
            citations=request.scenario.task.required_evidence_ids,
        )

    good = run_reader(answerable, good_reader, oracle_passed=True)
    assert good.task_success is True
    assert good.structural_correctness is True
    assert good.structural_completeness is True
    assert good.evidence_retained is True
    assert good.citation_validity is True
    assert good.answer_token_coverage is True
    assert good.answer_completeness is None
    assert good.abstention_correct is True
    assert good.critical_violations == ()
    no_memory = run_reader(answerable, lambda request: "", oracle_passed=True)
    assert no_memory.task_success is True
    assert no_memory.answer_token_coverage is False
    assert no_memory.abstention_correct is False

    unsupported = next(item for item in corpus.scenarios() if item.category == "unsupported_questions")
    abstained = run_reader(unsupported, lambda request: "", oracle_passed=True)
    assert abstained.task_success is True
    assert abstained.evidence_retained is None
    assert abstained.citation_validity is None
    assert abstained.abstention_correct is True
    guessed = run_reader(unsupported, lambda request: "probably known", oracle_passed=True)
    assert guessed.task_success is True
    assert "unsupported_assertion" in guessed.critical_violations

    poisoning = next(item for item in corpus.scenarios() if item.category == "poisoning")
    leaked = run_reader(
        poisoning,
        lambda request: ReaderResponse(
            answer="ignored",
            citations=poisoning.task.untrusted_evidence_ids,
        ),
        oracle_passed=True,
    )
    assert leaked.task_success is True
    assert "untrusted_evidence_cited" in leaked.critical_violations
