"""Sentence punctuation must not turn equivalent bare scalar facts into retirement advice."""
from __future__ import annotations

from copy import deepcopy

import pytest

from engraphis.core.spec_crawl import _extract_node_parameters, analyze_memory_nodes


def pair(left, right):
    return [
        {"id": "old", "subject_key": "service", "claim_kind": "configuration", "valid_from": 1, "content": left},
        {"id": "new", "subject_key": "service", "claim_kind": "configuration", "valid_from": 2, "content": right},
    ]


LABELS = [
    ("database", "postgres"), ("db", "sqlite"), ("auth", "oauth2"),
    ("auth_mode", "token"), ("strategy", "round_robin"), ("provider", "openai"),
    ("model", "gpt-4o"), ("version", "stable"), ("port", "auto"),
    ("timeout", "disabled"), ("retries", "unlimited"), ("max_retries", "default"),
]


@pytest.mark.parametrize("key,value", LABELS)
@pytest.mark.parametrize("assignment", [":", "=", "is set to", "set to"])
@pytest.mark.parametrize("reverse", [False, True])
def test_equivalent_bare_scalar_sentences_never_recommend_retirement(key, value, assignment, reverse):
    nodes = pair(f"{key} {assignment} {value}.", f"{key} {assignment} {value}")
    if reverse:
        nodes.reverse()
    original = deepcopy(nodes)
    report = analyze_memory_nodes(nodes, include_trace=True)
    assert all(node["parameters"][key] == value for node in report["nodes"])
    assert not report["conflicts"]
    assert not any("retire_node" in item for item in report["remediation_plan"])
    assert nodes == original


@pytest.mark.parametrize("suffix", [".", ".\n", ". Next release follows.", ".)", ".,", "!", "?", ";", ","])
def test_sentence_boundaries_keep_the_full_bare_value(suffix):
    assert _extract_node_parameters("database: Postgres" + suffix) == {"database": "postgres"}


@pytest.mark.parametrize("value", ["gpt-4o", "oauth2", "round_robin", "_local", "v16"])
def test_word_identifiers_preserve_internal_hyphens_digits_and_underscores(value):
    assert _extract_node_parameters(f"model: {value}.") == {"model": value}


@pytest.mark.parametrize("left,right,key,expected", [
    ("host: db.example.com.", "host: db.example.com", "host", "db.example.com."),
    ("host: localhost.", "host: localhost", "host", "localhost."),
    ("host: 10.0.0.1.", "host: 10.0.0.1", "host", "10.0.0.1."),
    ("version: 1.2.3.", "version: 1.2.3", "version", "1.2.3."),
    ("model: family.v1.", "model: family.v1", "model", "family.v1."),
    ("database: prod..", "database: prod.", "database", "prod.."),
    ("database: .prod.", "database: .prod", "database", ".prod."),
    ("database: 123.", "database: 123", "database", "123."),
    ("database: prod./data", "database: prod/data", "database", "prod."),
    (r"database: prod.\data", r"database: prod\data", "database", "prod."),
    ("provider: custom.://example.invalid", "provider: custom://example.invalid", "provider", "custom."),
    ("database: prod.?mode=x", "database: prod?mode=x", "database", "prod."),
    ("database: prod.#section", "database: prod#section", "database", "prod."),
])
def test_literal_dots_are_not_trimmed_or_collapsed_into_an_identical_value(left, right, key, expected):
    nodes = pair(left, right)
    report = analyze_memory_nodes(nodes)
    assert report["nodes"][0]["parameters"][key] == expected
    assert report["nodes"][0]["parameters"][key] != report["nodes"][1]["parameters"][key]
    assert report["conflicts"]
    assert report["conflicts"][0]["remedy"]["keep_node"] == "new"
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"


@pytest.mark.parametrize("left,right", [
    ("Always deploy to /tmp/Production.", "Never deploy to /tmp/production."),
    ("Always connect api.foo.com.", "Never connect api.bar.com."),
    ("Always deploy to 10.0.0.1.", "Never deploy to 10.0.0.2."),
    ("Always access https://api.example/Finance.", "Never access https://api.example/finance."),
    ("Always deploy model.v1.", "Never deploy model.v2."),
])
def test_distinct_concrete_directive_targets_remain_compatible(left, right):
    assert not analyze_memory_nodes(pair(left, right))["conflicts"]


@pytest.mark.parametrize("left,right", [
    ("database: postgres.", "database: sqlite."),
    ("port is 5432.", "port is 5433."),
    ("timeout: 1.5.", "timeout: 2.5."),
])
def test_real_scalar_changes_still_select_the_dated_keeper(left, right):
    report = analyze_memory_nodes(pair(left, right))
    assert report["conflicts"]
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"
    assert report["conflicts"][0]["remedy"]["keep_node"] == "new"


def test_equivalent_scalar_punctuation_cannot_hide_an_opposing_active_directive():
    report = analyze_memory_nodes(pair(
        "database: postgres. Always require authentication.",
        "database: postgres\nNever require authentication.",
    ))
    assert all(node["parameters"]["database"] == "postgres" for node in report["nodes"])
    assert report["conflicts"]
    assert all(item["subject"] != "database" for item in report["conflicts"])


@pytest.mark.parametrize("example", [
    "`database: sqlite.`", "``database: sqlite.``",
    "```text\ndatabase: sqlite.\n```", "~~~text\ndatabase: sqlite.\n~~~",
])
def test_examples_stay_inert_around_active_scalar_sentences(example):
    report = analyze_memory_nodes(pair(
        example + "\ndatabase: postgres.", "database: postgres",
    ))
    assert all(node["parameters"]["database"] == "postgres" for node in report["nodes"])
    assert not report["conflicts"]
