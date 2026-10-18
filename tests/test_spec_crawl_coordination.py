"""Coordinated actions retain their governor without inventing obligations."""
from __future__ import annotations

import copy

import pytest

from engraphis.core.spec_crawl import _extract_node_directives, analyze_memory_nodes


def _pair(left, right):
    return [
        {"id": "old", "subject_key": "service", "claim_kind": "policy",
         "valid_from": 1, "content": left},
        {"id": "current", "subject_key": "service", "claim_kind": "policy",
         "valid_from": 2, "content": right},
    ]


@pytest.mark.parametrize("left,right,target", [
    ("Must build and test.", "Never test.", "test"),
    ("Always test and build.", "Never build.", "build"),
    ("Must lint and build and test.", "Never test.", "test"),
    ("Must build and test the artifact.", "Never test artifact.", "test artifact"),
    ("Must build and carefully test.", "Never carefully test.", "carefully test"),
    ("Must build and test thoroughly.", "Never test thoroughly.", "test thoroughly"),
    ("Must build and read /etc/Production.", "Never read /etc/Production.", "read /etc/Production"),
    ("Must build and connect https://api.example/Finance.",
     "Never connect https://api.example/Finance.", "connect https://api.example/Finance"),
    ("Must build and deploy into production.", "Never deploy to production.", "deploy production"),
    ("Never build or test.", "Always test.", "test"),
    ("Don't build or test.", "Always test.", "test"),
    ("Mustn't build or test.", "Always test.", "test"),
    ("Must build and not test.", "Always test.", "test"),
    ("Must build and require authentication.", "Never require authentication.", "authentication"),
    ("Never build or require authentication.", "Always require authentication.", "authentication"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_coordinated_actions_prove_exact_dated_opposition(left, right, target, reverse):
    nodes = _pair(left, right)
    if reverse:
        nodes.reverse()
    before = copy.deepcopy(nodes)
    report = analyze_memory_nodes(nodes, include_trace=False)
    assert nodes == before
    assert report == analyze_memory_nodes(nodes, include_trace=False)
    assert len(report["conflicts"]) == report["conflicts_total"] == 1
    conflict = report["conflicts"][0]
    assert conflict["subject"] == target
    assert conflict["remedy"]["keep_node"] == "current"
    assert conflict["remedy"]["retire_node"] == "old"


@pytest.mark.parametrize("left,right", [
    ("Must build or test.", "Never build."),
    ("Must build or test.", "Never test."),
    ("Never build and test.", "Always build."),
    ("Never build and test.", "Always test."),
    ("Must build and test or lint.", "Never build."),
    ("Must build and may test.", "Never test."),
    ("Must build and should test.", "Never test."),
    ("Must build and can test.", "Never test."),
    ("Must build and not test.", "Never test."),
    ("May build and test.", "Never test."),
    ("Should build and test.", "Never test."),
    ("Must install build and test tools.", "Never test tools."),
    ("Must run build and test suites.", "Never test suites."),
    ("Must require authentication and authorization.", "Never use authorization."),
    ("Must deploy to build and test environments.", "Never test environments."),
    ("Must read /etc/build and /etc/test.", "Never test."),
    ("Must build and test in production.", "Never test in staging."),
    ("Must build and test via blue.", "Never test via green."),
    ("Must build and test with approval.", "Never test without approval."),
    ("Must build and read /etc/Production.", "Never read /etc/production."),
    ("Must build and connect https://api.example/Finance.",
     "Never connect https://api.example/finance."),
    ("We must build and reviewers test artifacts.", "Never test artifacts."),
    ("We must build and they test artifacts.", "Never test artifacts."),
    ("Must build and the tests pass.", "Never pass."),
    ("Must build when reviewers approve and test artifacts.", "Never test artifacts."),
    ("Must use artifacts that build and test cleanly.", "Never test cleanly."),
    ("Must build to review and test artifacts.", "Never test artifacts."),
    ("Must build. Test artifacts.", "Never test artifacts."),
    ("Must build; test artifacts.", "Never test artifacts."),
    ("Must build\ntest artifacts.", "Never test artifacts."),
    ("Example: `Must build and test.`", "Never test."),
    ("```text\nMust build and test.\n```", "Never test."),
])
def test_compatible_alternatives_subjects_objects_and_scopes_stay_live(left, right):
    report = analyze_memory_nodes(_pair(left, right), include_trace=False)
    assert not report["conflicts"]
    assert not any(item.get("action") == "supersede" for item in report["remediation_plan"])


@pytest.mark.parametrize("connector", ["and", "or"])
def test_whole_coordinated_expression_still_opposes_its_negation(connector):
    report = analyze_memory_nodes(_pair(
        f"Must build {connector} test.", f"Never build {connector} test."), include_trace=False)
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"


@pytest.mark.parametrize("field", ["subject_key", "claim_kind"])
def test_coordinated_actions_preserve_metadata_partitions(field):
    nodes = _pair("Must build and test.", "Never test.")
    nodes[1][field] = "different"
    assert not analyze_memory_nodes(nodes)["conflicts"]


def test_long_coordinated_scope_keeps_full_literal_identity():
    target = "https://api.example/" + "segment/" * 30
    parsed = _extract_node_directives("Must build and connect " + target + "one.")
    assert any("connect:" in key for key in parsed)
    assert all(len(key) < 125 for key in parsed)
    report = analyze_memory_nodes(_pair(
        "Must build and connect " + target + "one.",
        "Never connect " + target + "two."))
    assert not report["conflicts"]
