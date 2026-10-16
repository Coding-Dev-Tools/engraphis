"""Contradiction and similarity evidence must come from active memory prose."""
from __future__ import annotations

import copy

import pytest

from engraphis.core import spec_crawl
from engraphis.core.textutil import jaccard, tokenize


@pytest.mark.parametrize("claim_kind", ["property", "status", "deployment"])
@pytest.mark.parametrize("reverse", [False, True])
def test_complementary_same_subject_facts_do_not_create_conflicts(claim_kind, reverse):
    nodes = [
        {"id": "encrypted", "subject_key": "database", "claim_kind": claim_kind,
         "content": "The database is encrypted at rest."},
        {"id": "replicated", "subject_key": "database", "claim_kind": claim_kind,
         "content": "The database is replicated across zones."},
    ]
    if reverse:
        nodes.reverse()
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert all(not node["parameters"] and not node["directives"] for node in report["nodes"])
    assert not report["conflicts"]
    assert report["score_breakdown"]["conflict_penalty"] == 0
    assert report["links"][0]["relation"] == "shared_subject"
    untyped = spec_crawl.analyze_memory_nodes([{**node, "claim_kind": ""} for node in nodes])
    assert report["cluster_health_score"] == untyped["cluster_health_score"]
    assert not any(item["action"] in {"clarify", "supersede"} for item in report["remediation_plan"])


@pytest.mark.parametrize("left,right", [
    ("host: primary.example", "port: 5432"),
    ("Always deploy to production.", "Never access production logs."),
    ("host: primary.example. Data retention lasts months.", "host: primary.example. Replicas span zones."),
])
def test_nonempty_but_compatible_evidence_does_not_become_a_subject_only_conflict(left, right):
    nodes = [{"id": "left", "subject_key": "database", "claim_kind": "property", "content": left},
             {"id": "right", "subject_key": "database", "claim_kind": "property", "content": right}]
    assert not spec_crawl.analyze_memory_nodes(nodes)["conflicts"]


@pytest.mark.parametrize("left,right,subject", [
    ("port: 5432", "port: 5433", "port"),
    ("host: primary.example", "host: secondary.example", "host"),
    ("Always deploy to production.", "Never deploy to production.", "deploy to production"),
    ("Always access production logs.", "Never access production logs.", "access production logs"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_contradiction_evidence_remains_actionable(left, right, subject, reverse):
    nodes = [{"id": "old", "subject_key": "service", "claim_kind": "policy", "valid_from": 1, "content": left},
             {"id": "current", "subject_key": "service", "claim_kind": "policy", "valid_from": 2, "content": right}]
    if reverse:
        nodes.reverse()
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["subject"] == subject
    assert report["conflicts"][0]["remedy"]["keep_node"] == "current"
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"


def _code(kind, text):
    if kind == "backtick_fence":
        return "```text\n" + text + "\n```"
    if kind == "tilde_fence":
        return "~~~~text\n" + text + "\n~~~~"
    if kind == "inline":
        return "`" + text + "`"
    return "``literal ` " + text + "``"


@pytest.mark.parametrize("kind", ["backtick_fence", "tilde_fence", "inline", "multi_inline"])
@pytest.mark.parametrize("example_words", [200, 1200])
def test_shared_examples_cannot_link_or_consolidate_unrelated_policies(kind, example_words):
    shared = _code(kind, " ".join(f"setting{index}" for index in range(example_words)))
    nodes = [
        {"id": "garden", "title": "Garden", "content": "Irrigate seedlings during spring.\n" + shared},
        {"id": "space", "title": "Orbit", "content": "Calibrate satellite trajectory monthly.\n" + shared},
    ]
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert not report["links"]
    assert not report["redundancies"]
    assert all(item["suggested_link_to"] is None and item["similarity"] == 0 for item in report["orphans"])
    assert report["similarity_sampled_node_count"] == 0
    assert all(not node["similarity_sampled"] and node["similarity_token_count"] == 5 for node in report["nodes"])
    assert not any(item["action"] in {"link", "consolidate"} for item in report["remediation_plan"])


@pytest.mark.parametrize("kind", ["backtick_fence", "tilde_fence", "inline", "multi_inline"])
def test_different_examples_cannot_dilute_real_prose_duplicates(kind):
    prose = "Run release verification suite before deployment."
    nodes = [{"id": name, "title": "Release checklist",
              "content": prose + "\n" + _code(kind, " ".join(f"{name}{index}" for index in range(600)))}
             for name in ("alpha", "omega")]
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert report["links"][0]["similarity"] == 1
    assert len(report["redundancies"]) == 1
    remedy = report["redundancies"][0]["remedy"]
    assert remedy["action"] == "consolidate"
    assert "keep_node" not in remedy and "retire_node" not in remedy


@pytest.mark.parametrize("kind", ["backtick_fence", "multi_inline"])
def test_word_partial_examples_cannot_hide_a_full_text_parameter_conflict(kind):
    example = _code(kind, "port: 9000 " + " ".join(f"neutral{index}" for index in range(6000)))
    nodes = [{"id": "old", "subject_key": "service", "claim_kind": "port", "valid_from": 1,
              "content": example + "\nport: 5432"},
             {"id": "current", "subject_key": "service", "claim_kind": "port", "valid_from": 2,
              "content": example + "\nport: 5433"}]
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert report["word_analysis_truncated_node_count"] == 2
    assert report["similarity_sampled_node_count"] == 0
    assert [node["parameters"] for node in report["nodes"]] == [{"port": "5432"}, {"port": "5433"}]
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["remedy"]["keep_node"] == "current"


def test_inline_examples_in_titles_cannot_supply_shared_similarity_tokens():
    example = "``literal ` " + " ".join(f"setting{index}" for index in range(200)) + "``"
    report = spec_crawl.analyze_memory_nodes([
        {"id": "garden", "title": "Garden " + example, "content": "Irrigate seedlings during spring."},
        {"id": "space", "title": "Orbit " + example, "content": "Calibrate satellite trajectory monthly."},
    ])
    assert not report["links"] and not report["redundancies"]
    assert all(node["similarity_token_count"] == 5 for node in report["nodes"])


@pytest.mark.parametrize("backslashes", [1, 3])
def test_escaped_literal_delimiters_still_contribute_active_prose(backslashes):
    delimiter = "\\" * backslashes + "`"
    content = delimiter + " ".join(f"active{index}" for index in range(100)) + delimiter
    report = spec_crawl.analyze_memory_nodes([
        {"id": "left", "title": "Alpha", "content": content},
        {"id": "right", "title": "Omega", "content": content},
    ])
    assert report["links"][0]["similarity"] > 0.95
    assert report["redundancies"]


def test_code_only_nodes_have_empty_similarity_sets_without_division_errors():
    report = spec_crawl.analyze_memory_nodes([
        {"id": "left", "title": "`Alpha`", "content": "```text\nshared example\n```"},
        {"id": "right", "title": "`Omega`", "content": "```text\nshared example\n```"},
    ])
    assert all(node["similarity_token_count"] == 0 for node in report["nodes"])
    assert not report["links"] and not report["redundancies"]
    assert all(item["suggested_link_to"] is None for item in report["orphans"])


def test_masked_similarity_remains_exact_when_active_overlap_exists():
    nodes = [
        {"id": "left", "title": "Authentication", "content": "Verify token authentication.\n``noise ` first``"},
        {"id": "right", "title": "Authentication", "content": "Verify token authentication before deployment.\n`second noise`"},
    ]
    expected = jaccard(tokenize("Authentication Verify token authentication."),
                       tokenize("Authentication Verify token authentication before deployment."))
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert report["links"][0]["similarity"] == round(expected, 2)


def test_audit_preserves_declared_links_and_never_mutates_inputs():
    nodes = [
        {"id": "left", "title": "Garden", "subject_key": "garden", "claim_kind": "property",
         "content": "Irrigate seedlings during spring.\n`shared example`"},
        {"id": "right", "title": "Orbit", "subject_key": "space", "claim_kind": "property",
         "content": "Calibrate satellite trajectory monthly.\n`shared example`"},
    ]
    links = [{"a": "left", "b": "right", "relation": "depends_on"}]
    original = copy.deepcopy((nodes, links))
    report = spec_crawl.analyze_memory_nodes(nodes, links)
    assert (nodes, links) == original
    assert report["links"] == [{"a": "left", "b": "right", "relation": "depends_on", "inferred": False}]
    assert all(node["degree"] == 1 and not node["is_orphan"] for node in report["nodes"])
