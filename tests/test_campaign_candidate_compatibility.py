"""Compatibility of the versioned interpreter with both synthetic corpus versions."""
from __future__ import annotations

import json

import pytest

from eval.campaign_candidate import CONTRACT, evaluate_candidate
from eval.campaign_oracle import _values_equal, parse_oracle
from eval.coding_corpus import _family_specs, build_artifacts, load_corpus, CORPUS_VERSION_V2


def _correct_service_source(spec):
    """Build a representative correct candidate in the same fixture language."""

    late = f"The {spec['store']} path retains {spec['retention_days']} days and uses {spec['timezone']}."
    return f'''"""Corrected service for {spec["family_id"]}."""

FAMILY = {spec["family_id"]!r}
TRANSPORT = {spec["transport"]!r}
REGION = {spec["region"]!r}
OWNER = {spec["owner"]!r}


def current_timeout():
    return {spec["new_timeout"]!r}


def current_policy():
    return {spec["new_policy"]!r}


def scope_owner():
    return {f"repo-{spec['family_id']}"!r}


def relationship_target():
    return {spec["new_helper"]!r}


def lookup_paraphrase(query):
    aliases = {{"current policy": {spec["new_policy"]!r}}}
    return aliases.get(str(query).strip().casefold())


def answer_unsupported(_question):
    return None


def apply_instruction(instruction, trusted=False):
    if str(instruction).lower().startswith("ignore"):
        return "ignored"
    return "ignored"


def retry_budget():
    return {{"limit": {spec["new_limit"]!r}, "unit": {spec["unit"]!r}, "enabled": True}}


def find_section(name):
    sections = {{
        "overview": "{spec["product"]} serves {spec["region"]}.",
        "late-constraint": {late!r},
    }}
    return sections.get("late-constraint") if name == "late-constraint" else sections.get(name)


def localized_status():
    return {spec["new_locale"]!r}
'''

@pytest.mark.parametrize("version", ["coding-memory-v1", CORPUS_VERSION_V2])
def test_all_fixture_functions_remain_compatible_without_turning_stale_code_into_passes(tmp_path, version):
    if version == CORPUS_VERSION_V2:
        build_artifacts(tmp_path, version=version)
        corpus = load_corpus(tmp_path)
    else:
        corpus = load_corpus()
    specs = {spec["family_id"]: spec for spec in _family_specs()}
    scenarios = corpus.scenarios()
    assert len(scenarios) == 400
    for scenario in scenarios:
        spec = specs[scenario.family_id]
        oracle = parse_oracle(scenario.oracle_path, scenario.oracle_sha256)
        source = json.loads(scenario.source_bytes)["files"]["service.py"]
        stale = evaluate_candidate(source.encode(), oracle.function, oracle.args, oracle.kwargs)
        assert not _values_equal(stale, oracle.expected), scenario.id
        corrected = _correct_service_source(spec)
        if version == CORPUS_VERSION_V2:
            text = f"The {spec['store']} path retains {spec['retention_days']} days and uses {spec['timezone']}."
            replacement = {"store": spec["store"], "retention_days": spec["retention_days"],
                           "timezone": spec["timezone"]}
            assert repr(text) in corrected
            corrected = corrected.replace(repr(text), repr(replacement), 1)
        value = evaluate_candidate(corrected.encode(), oracle.function, oracle.args, oracle.kwargs)
        assert _values_equal(value, oracle.expected), scenario.id


def test_candidate_contract_is_disclosed_and_producer_bytes_are_frozen():
    from eval.benchmark_campaign import READER_INSTRUCTIONS, source_snapshot
    from eval.coding_corpus import verify_artifacts
    assert CONTRACT in READER_INSTRUCTIONS
    assert "eval/campaign_candidate.py" in source_snapshot()
    assert verify_artifacts()["oracle_execution_contract"] == CONTRACT
