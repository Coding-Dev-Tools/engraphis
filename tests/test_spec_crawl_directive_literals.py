"""Negation spelling and concrete targets must not change directive identity."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from engraphis.core.spec_crawl import _extract_node_directives, analyze_memory_nodes


NEGATIVES = [
    (form.replace("'", apostrophe), expanded)
    for form, expanded in [
        ("Don't", "Do not"), ("Doesn't", "Does not"), ("Mustn't", "Must not"),
        ("Shouldn't", "Should not"), ("Can't", "Can not"),
    ]
    for apostrophe in ("'", "\u2019")
] + [("Cannot", "Can not")]


def _pair(left, right):
    return [{"id": "old", "subject_key": "service", "claim_kind": "policy", "valid_from": 1, "content": left},
            {"id": "current", "subject_key": "service", "claim_kind": "policy", "valid_from": 2, "content": right}]


@pytest.mark.parametrize("negative,expanded", NEGATIVES)
@pytest.mark.parametrize("reverse", [False, True])
def test_contracted_negations_match_uncontracted_policy_and_still_conflict(negative, expanded, reverse):
    left = negative + " deploy production."
    assert _extract_node_directives(left) == _extract_node_directives(expanded + " deploy production.")
    nodes = _pair(left, "Always deploy production.")
    if reverse:
        nodes.reverse()
    report = analyze_memory_nodes(nodes)
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["subject"] == "deploy production"
    assert report["conflicts"][0]["remedy"]["keep_node"] == "current"
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"


@pytest.mark.parametrize("negative,expanded", NEGATIVES)
@pytest.mark.parametrize("verb", ["require", "enforce"])
def test_nested_negative_requirements_do_not_invert_into_enforcement(negative, expanded, verb):
    text = f"{negative} {verb} authentication."
    assert _extract_node_directives(text) == {"authentication": "forbid"}
    assert _extract_node_directives(text) == _extract_node_directives(f"{expanded} {verb} authentication.")
    assert not analyze_memory_nodes(_pair(text, "Never require authentication."))["conflicts"]


@pytest.mark.parametrize("negative,expanded", NEGATIVES)
def test_negated_negative_action_keeps_existing_nested_semantics(negative, expanded):
    text = negative + " skip authentication."
    assert _extract_node_directives(text) == _extract_node_directives(expanded + " skip authentication.")
    assert _extract_node_directives(text) == {"authentication": "enforce"}
    assert not analyze_memory_nodes(_pair(text, "Always require authentication."))["conflicts"]


@pytest.mark.parametrize("separator", [". ", "; ", ", ", "\n", " and always "])
def test_contracted_negation_does_not_leak_across_a_clause_or_conjunction(separator):
    assert _extract_node_directives("Don't deploy production" + separator + "require authentication.") == {
        "deploy production": "forbid", "authentication": "enforce",
    }


@pytest.mark.parametrize("left,right", [
    ("connect api.foo.com", "connect api.bar.com"),
    ("require authentication at api.foo.com", "require authentication at api.bar.com"),
    ("deploy to 10.0.0.1", "deploy to 10.0.0.2"),
    ("connect api.foo.com:443", "connect api.foo.com:8443"),
    ("connect https://api.foo.com/v1", "connect https://api.foo.com/v2"),
    ("connect https://api.foo.com/v1?q=one", "connect https://api.foo.com/v1?q=two"),
    ("connect https://api.foo.com/v1;scope=one", "connect https://api.foo.com/v1;scope=two"),
    ("read /etc/service/prod.json", "read /etc/service/stage.json"),
    ("read config/prod.json", "read config/stage.json"),
    (r"read C:\service\prod.json", r"read C:\service\stage.json"),
    ("deploy on 2026-10-16", "deploy on 2026-10-17"),
    ("connect [2001:db8::1]:443", "connect [2001:db8::2]:443"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_different_concrete_targets_cannot_recommend_retiring_a_compatible_policy(left, right, reverse):
    nodes = _pair("Always " + left + ".", "Never " + right + ".")
    if reverse:
        nodes.reverse()
    report = analyze_memory_nodes(nodes)
    assert not report["conflicts"]
    assert not any("retire_node" in item for item in report["remediation_plan"])
    assert not set(report["nodes"][0]["directives"]) & set(report["nodes"][1]["directives"])


@pytest.mark.parametrize("target", [
    "connect api.foo.com", "require authentication at api.foo.com", "deploy to 10.0.0.1",
    "connect api.foo.com:443", "connect https://api.foo.com/v1?q=one",
    "read /etc/service/prod.json", "read config/prod.json", r"read C:\service\prod.json",
    "deploy on 2026-10-16", "connect [2001:db8::1]:443",
])
def test_identical_concrete_targets_still_prove_a_dated_opposition(target):
    report = analyze_memory_nodes(_pair("Always " + target + ".", "Don't " + target + "."))
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["remedy"]["keep_node"] == "current"
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"


def test_sentence_punctuation_remains_distinct_from_qualified_target_punctuation():
    assert _extract_node_directives("Always connect api.foo.com. Never connect api.bar.com.") == {
        "connect api.foo.com": "enforce", "connect api.bar.com": "forbid",
    }
    assert _extract_node_directives("Always connect https://api.foo.com/v1?q=one. Never deploy to 10.0.0.2.") == {
        "connect https://api.foo.com/v1?q=one": "enforce", "deploy to 10.0.0.2": "forbid",
    }


def test_qualified_targets_keep_complete_hashed_identity_beyond_display_limit():
    base = "https://api.foo.com/" + "segment/" * 30
    nodes = _pair("Always connect " + base + "one.", "Never connect " + base + "two.")
    report = analyze_memory_nodes(nodes)
    assert not report["conflicts"]
    assert all(len(key) < 125 for node in report["nodes"] for key in node["directives"])


def test_literal_contraction_spelling_in_a_url_is_not_rewritten_as_policy():
    text = "Always connect https://example.test/don't?scope=one."
    assert _extract_node_directives(text) == {"connect https://example.test/don't?scope=one": "enforce"}


def test_new_policy_analysis_keeps_input_records_read_only():
    nodes = _pair("Don't deploy to 10.0.0.1.", "Always deploy to 10.0.0.2.")
    original = copy.deepcopy(nodes)
    assert not analyze_memory_nodes(nodes)["conflicts"]
    assert nodes == original


@pytest.mark.parametrize("left,right", [
    ("read /tmp/Production", "read /tmp/production"),
    ("read config/Production.json", "read config/production.json"),
    (r"read C:\service\Production.json", r"read C:\service\production.json"),
    ("connect https://api.example/Finance", "connect https://api.example/finance"),
    ("connect https://api.example/path?scope=Finance", "connect https://api.example/path?scope=finance"),
    ("read .config/Finance.json", "read .config/finance.json"),
])
def test_case_sensitive_resource_spelling_never_collapses_into_supersession(left, right):
    report = analyze_memory_nodes(_pair("Always " + left + ".", "Never " + right + "."))
    assert not report["conflicts"]
    assert not set(report["nodes"][0]["directives"]) & set(report["nodes"][1]["directives"])


def test_plain_governing_action_and_natural_scope_words_remain_case_insensitive():
    report = analyze_memory_nodes(_pair("ALWAYS DEPLOY PRODUCTION.", "DoN’T deploy production."))
    assert report["conflicts"][0]["subject"] == "deploy production"


@pytest.mark.parametrize("fixture", ["long_word", "qualified_runs", "many_negations", "vague_gap", "dot_run", "hyphen_run"])
def test_near_limit_parsing_is_bounded_without_losing_identity_or_flags(fixture):
    program = r'''
import json,sys,time
from engraphis.core.spec_crawl import _extract_node_directives,crawl_spec
kind=sys.argv[1]
if kind=='long_word':
    text='Always '+'a'*60000
elif kind=='qualified_runs':
    text='Always connect '+'a.'*27000+'example.'
elif kind=='many_negations':
    text="Don't deploy prod. "*3000
elif kind=='dot_run':
    text='Always '+'.'*60000+' Never deploy production.'
elif kind=='hyphen_run':
    text='Always '+'-'*60000+' Never deploy production.'
else:
    text='# Rules\nall'+' '*60000+'release443'
start=time.perf_counter()
directives=_extract_node_directives(text)
assert all(len(key)<125 for key in directives)
if kind=='vague_gap':
    report=crawl_spec(text)
    assert any(flag['token']=='all' for flag in report['flags'])
else:
    assert directives
print(json.dumps({'chars':len(text),'seconds':time.perf_counter()-start,'max_key_chars':max(map(len,directives),default=0)}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", program, fixture], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert 50000 < result["chars"] < 64000
    assert result["seconds"] < 4
