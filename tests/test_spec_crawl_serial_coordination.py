"""Serial action lists retain Boolean, grammatical and concrete-scope boundaries."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

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
    ("Must build, test, and deploy.", "Never build.", "build"),
    ("Must build, test, and deploy.", "Never test.", "test"),
    ("Must build, test, and deploy.", "Never deploy.", "deploy"),
    ("Must build, test and deploy.", "Never test.", "test"),
    ("Must build, test and deploy.", "Never deploy.", "deploy"),
    ("Must build, test.", "Never test.", "test"),
    ("Must build, test, deploy.", "Never deploy.", "deploy"),
    ("Must build, and test.", "Never test.", "test"),
    ("Never build, or test.", "Always build.", "build"),
    ("Never build, or test.", "Always test.", "test"),
    ("Must build, test, deploy, and lint.", "Never lint.", "lint"),
    ("Never build, test, or deploy.", "Always test.", "test"),
    ("Never build, test or deploy.", "Always deploy.", "deploy"),
    ("Don't build, test, or deploy.", "Always test.", "test"),
    ("Mustn't build, test, or deploy.", "Always deploy.", "deploy"),
    ("Must build, test, and not deploy.", "Always deploy.", "deploy"),
    ("Must build, test, and require authentication.",
     "Never require authentication.", "authentication"),
    ("Must build, test, and carefully deploy.", "Never carefully deploy.", "carefully deploy"),
    ("Must build, test, and deploy thoroughly.", "Never deploy thoroughly.", "deploy thoroughly"),
    ("Must build, test, and deploy into production.", "Never deploy to production.", "deploy production"),
    ("Must build, test, and read /etc/Production.", "Never read /etc/Production.", "read /etc/Production"),
    ("Must build, test, and connect https://api.example/Finance.",
     "Never connect https://api.example/Finance.", "connect https://api.example/Finance"),
    ("Must build, always test.", "Never test.", "test"),
    ("Must carefully build, thoroughly test, and safely deploy.",
     "Never safely deploy.", "safely deploy"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_serial_actions_prove_only_entailed_dated_opposition(left, right, target, reverse):
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
    ("Must build, test, or deploy.", "Never build."),
    ("Must build, test, or deploy.", "Never test."),
    ("Must build, test, or deploy.", "Never deploy."),
    ("Must build, or test.", "Never build."),
    ("Must build, or test.", "Never test."),
    ("Never build, and test.", "Always build."),
    ("Never build, and test.", "Always test."),
    ("Never build, test, and deploy.", "Always build."),
    ("Never build, test, and deploy.", "Always test."),
    ("Never build, test.", "Always build."),
    ("Never build, test.", "Always test."),
    ("Must build, test and deploy or lint.", "Never build."),
    ("Must build and test, or deploy.", "Never build."),
    ("Must build, test cases, or deploy.", "Never build."),
    ("Must build, test, or reviewers deploy artifacts.", "Never build."),
    ("Must build, test or may deploy.", "Never build."),
    ("Must build, test, or may deploy.", "Never build."),
    ("Must build, may test.", "Never test."),
    ("Must build, should test.", "Never test."),
    ("Must build, can test.", "Never test."),
    ("Must build, reviewers test.", "Never test."),
    ("Must build, the reviewers test.", "Never test."),
    ("Must build, if reviewers test, and deploy.", "Never test."),
    ("Must build when reviewers approve, test, and deploy.", "Never test."),
    ("Must install build, test, and deploy tools.", "Never test."),
    ("Must run build, test, and deploy suites.", "Never deploy suites."),
    ("Must read notes, test cases, and deploy docs.", "Never test cases."),
    ("Must require authentication, authorization, and logging.", "Never use authorization."),
    ("Must deploy to build, test, and staging environments.", "Never test."),
    ("Must build in staging, test, and deploy.", "Never test."),
    ("Must build, test; deploy.", "Never deploy."),
    ("Must build, test\ndeploy.", "Never deploy."),
    ("Must, test and deploy.", "Never test."),
    ("Must build,, test.", "Never test."),
    ("Must build, test, and read /etc/Production.", "Never read /etc/production."),
    ("Must build, test, and connect https://api.example/Finance.",
     "Never connect https://api.example/finance."),
    ("Must build, test, and deploy from production.", "Never deploy to production."),
    ("Must build, test, and deploy via blue.", "Never deploy via green."),
    ("Must build, test, and deploy without approval.", "Never deploy with approval."),
    ("Must deploy, build, and connect into staging.", "Never deploy, build, and connect to staging."),
    ("Must build, test, and read /etc/build,test.", "Never read /etc/build,deploy."),
    ("Must build, test, and connect https://api.example/build,test.",
     "Never connect https://api.example/build,deploy."),
    ("Example: `Must build, test, and deploy.`", "Never test."),
    ("```text\nMust build, test, and deploy.\n```", "Never test."),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_serial_alternatives_objects_clauses_and_literals_do_not_invent_obligations(left, right, reverse):
    nodes = _pair(left, right)
    if reverse:
        nodes.reverse()
    report = analyze_memory_nodes(nodes, include_trace=False)
    assert not report["conflicts"]
    assert not any(item.get("action") == "supersede" for item in report["remediation_plan"])


@pytest.mark.parametrize("left,right", [
    ("Must build, test, and deploy.", "Never build and test and deploy."),
    ("Must build, test and deploy.", "Never build, test, and deploy."),
    ("Must build, test, or deploy.", "Never build or test or deploy."),
    ("Must build, test or deploy.", "Never build, test, or deploy."),
    ("Must build, test, deploy.", "Never build and test and deploy."),
])
def test_serial_punctuation_preserves_opposition_to_the_same_whole_expression(left, right):
    report = analyze_memory_nodes(_pair(left, right))
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"


def test_serial_and_or_groups_keep_different_boolean_identity():
    assert not analyze_memory_nodes(_pair(
        "Must build, test, or deploy.", "Never build, test, and deploy."))["conflicts"]
    assert analyze_memory_nodes(_pair(
        "Must build, test, and deploy.", "Never build, test, or deploy."))["conflicts"]


@pytest.mark.parametrize("field", ["subject_key", "claim_kind"])
def test_serial_actions_preserve_metadata_partitions(field):
    nodes = _pair("Must build, test, and deploy.", "Never test.")
    nodes[1][field] = "different"
    assert not analyze_memory_nodes(nodes)["conflicts"]


def test_serial_long_literal_identity_and_original_standalone_keys_stay_exact():
    target = "https://api.example/" + "segment/" * 30
    parsed = _extract_node_directives("Must build, test, and connect " + target + "one.")
    assert _extract_node_directives("Always connect " + target + "one.").items() <= parsed.items()
    assert all(len(key) < 125 for key in parsed)
    assert not analyze_memory_nodes(_pair(
        "Must build, test, and connect " + target + "one.",
        "Never connect " + target + "two."))["conflicts"]


@pytest.mark.parametrize("governor,connector,polarity", [
    ("Must", "and", "enforce"), ("Never", "or", "forbid"),
])
def test_near_limit_serial_lists_are_bounded_and_retain_last_action(governor, connector, polarity):
    program = r'''
import json,sys,time
from engraphis.core.spec_crawl import _extract_node_directives
text=sys.argv[1]+' '+'build, '*8000+'test, '+sys.argv[2]+' deploy.'
start=time.perf_counter()
directives=_extract_node_directives(text)
assert directives['build']==directives['test']==directives['deploy']==sys.argv[3]
assert all(len(key)<125 for key in directives)
print(json.dumps({'chars':len(text),'seconds':time.perf_counter()-start}))
'''
    result = subprocess.run(
        [sys.executable, "-c", program, governor, connector, polarity],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    timings = json.loads(result.stdout)
    assert 50000 < timings["chars"] < 64000
    assert timings["seconds"] < 4
