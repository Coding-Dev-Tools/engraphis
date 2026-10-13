"""Unit tests for multi-node memory analysis in engraphis.core.spec_crawl."""
from __future__ import annotations

import pytest

from engraphis.core.spec_crawl import analyze_memory_nodes


def test_analyze_empty_memory_nodes():
    res = analyze_memory_nodes([])
    assert res["cluster_health_score"] == 0
    assert res["node_count"] == 0
    assert len(res["policy_gaps"]) == 7
    assert len(res["conflicts"]) == 0


def test_cross_node_parameter_conflict_detection():
    memories = [
        {
            "id": "mem_db_v1",
            "title": "Primary Database Config",
            "content": "The main database is set to postgres. Port is 5432.",
            "mtype": "semantic",
            "subject_key": "db.config",
            "ingested_at": 1000.0,
            "valid_from": 1000.0,
        },
        {
            "id": "mem_db_v2",
            "title": "Updated Database Config",
            "content": "The main database is set to sqlite. Port is 5432.",
            "mtype": "semantic",
            "subject_key": "db.config",
            "ingested_at": 2000.0,
            "valid_from": 2000.0,
        },
    ]
    report = analyze_memory_nodes(memories)
    assert report["node_count"] == 2
    assert len(report["conflicts"]) >= 1

    conflict = report["conflicts"][0]
    assert conflict["subject"] in {"db.config", "database"}
    assert conflict["remedy"]["action"] == "supersede"
    # Newer node should be retained, older should be retired
    assert conflict["remedy"]["keep_node"] == "mem_db_v2"
    assert conflict["remedy"]["retire_node"] == "mem_db_v1"


def test_cross_node_directive_conflict_detection():
    memories = [
        {
            "id": "mem_rule_auth_jwt",
            "title": "Authentication Rule",
            "content": "We must enforce token verification on all incoming requests.",
            "mtype": "procedural",
            "ingested_at": 1000.0,
        },
        {
            "id": "mem_rule_auth_skip",
            "title": "Legacy Auth Rule",
            "content": "Developers may disable token verification for local testing.",
            "mtype": "procedural",
            "ingested_at": 1500.0,
        },
    ]
    report = analyze_memory_nodes(memories)
    assert len(report["conflicts"]) >= 1
    conflict = report["conflicts"][0]
    assert "token" in conflict["subject"] or "verification" in conflict["subject"]


def test_orphan_node_detection_and_link_suggestion():
    memories = [
        {
            "id": "mem_core_engine",
            "title": "Memory Engine Architecture",
            "content": "The engine orchestrates vector recall, sqlite store, and scoring fusion.",
            "mtype": "semantic",
        },
        {
            "id": "mem_vector_store",
            "title": "Vector Index Backend",
            "content": "Vector index backend manages cosine similarity and sqlite store vectors.",
            "mtype": "semantic",
        },
        {
            "id": "mem_pricing",
            "title": "Hosted Cloud Billing",
            "content": "Cloud members pay subscription tiers with monthly credit allowances.",
            "mtype": "semantic",
        },
    ]
    # Declared link between engine and vector store, leaving pricing as an orphan
    links = [{"a": "mem_core_engine", "b": "mem_vector_store", "relation": "subsystem"}]
    report = analyze_memory_nodes(memories, links=links)

    assert report["node_count"] == 3
    orphan_ids = [o["node_id"] for o in report["orphans"]]
    assert "mem_pricing" in orphan_ids

    orphan_meta = next(o for o in report["orphans"] if o["node_id"] == "mem_pricing")
    assert orphan_meta["node_id"] == "mem_pricing"


def test_multi_node_radar_and_policy_gaps():
    memories = [
        {
            "id": "mem_role",
            "title": "Agent Role and Persona",
            "content": "You are a senior systems engineer acting as the repository maintainer.",
            "mtype": "semantic",
        },
        {
            "id": "mem_rule",
            "title": "Safety Guidelines",
            "content": "Never overwrite memory records. Truth is bi-temporal.",
            "mtype": "procedural",
        },
    ]
    report = analyze_memory_nodes(memories)
    # Total 7 axes: role and rules are covered; review, teamwork, etc. are missing
    assert report["coverage"]["role"] > 0
    assert report["coverage"]["rules"] > 0
    assert report["coverage"]["review"] == 0.0

    gap_axes = [g["axis"] for g in report["policy_gaps"]]
    assert "review" in gap_axes
    assert "team" in gap_axes
    assert any(g["remedy"]["action"] == "create_memory" for g in report["policy_gaps"])


def test_simulation_trace_generation():
    memories = [
        {"id": "mem_1", "title": "Architecture", "content": "FastAPI with SQLite."},
        {"id": "mem_2", "title": "Commands", "content": "Run pytest to verify all tests."},
    ]
    report = analyze_memory_nodes(memories, include_trace=True)
    assert len(report["trace"]) > 0
    verbs = [step["verb"] for step in report["trace"]]
    assert "node_visit" in verbs
    assert "node_classify" in verbs


def test_memory_nodes_classify_content_and_bound_displayed_flags():
    from engraphis.core.spec_crawl import MAX_FLAGS
    report = analyze_memory_nodes([
        {"id": "a", "content": "Check every result carefully."},
        {"id": "b", "content": "every " * 400},
    ])
    node = report["nodes"][0]
    assert node["word_count"] > 0
    assert node["token_kinds"]["vague"] > 0
    assert node["flag_count"] > 0
    assert any(flag["node_id"] == "a" for flag in report["flags"])
    assert len(report["flags"]) <= MAX_FLAGS


def test_equivalent_negated_directives_do_not_conflict():
    for rule in ("Do not deploy changes.", "You must not deploy changes.",
                 "You must not enforce deployment."):
        target = "deployment" if "enforce" in rule else "deploy"
        report = analyze_memory_nodes([
            {"id": "a", "content": f"Never {target} changes."},
            {"id": "b", "content": rule},
        ])
        assert not report["conflicts"]
    positive = analyze_memory_nodes([
        {"id": "a", "content": "Do not deploy changes."},
        {"id": "b", "content": "You must deploy changes."},
    ])
    assert positive["conflicts"]
    assert all(conflict["remedy"]["action"] == "clarify" for conflict in positive["conflicts"])
    assert all("keep_node" not in conflict["remedy"] and "retire_node" not in conflict["remedy"]
               for conflict in positive["conflicts"])


def test_complementary_claims_and_distinct_subjects_remain_live():
    for nodes in (
        [{"id": "a", "subject_key": "db", "claim_kind": "host", "content": "host: primary.example"},
         {"id": "b", "subject_key": "db", "claim_kind": "port", "content": "port: 5432"}],
        [{"id": "a", "subject_key": "prod", "content": "port: 5432"},
         {"id": "b", "subject_key": "dev", "content": "port: 8000"}],
        [{"id": "a", "subject_key": "prod", "content": "Never deploy changes."},
         {"id": "b", "subject_key": "dev", "content": "Always deploy changes."}],
    ):
        assert not analyze_memory_nodes(nodes)["conflicts"]


@pytest.mark.parametrize("keys", [({}, {}), ({"subject_key": "web"}, {}),
                                     ({}, {"subject_key": "db"}),
                                     ({"claim_kind": "port"}, {"claim_kind": "port"})])
def test_unrelated_parameter_values_need_positive_subject_identity(keys):
    nodes = [{"id": "web", "content": "Web server port: 8000", "valid_from": 2000, **keys[0]},
             {"id": "db", "content": "Database port: 5432", "valid_from": 1000, **keys[1]}]
    for order in (nodes, list(reversed(nodes))):
        assert not analyze_memory_nodes(order)["conflicts"]


@pytest.mark.parametrize("kind", ["port", ""])
def test_subject_partition_preserves_real_parameter_conflicts_amid_unrelated_values(kind):
    nodes = [{"id": "old", "subject_key": "db.primary", "claim_kind": "port",
              "content": "port: 5432", "valid_from": 1000},
             {"id": "current", "subject_key": "db.primary", "claim_kind": kind,
              "content": "port: 5433", "valid_from": 2000}]
    nodes.extend({"id": f"other_{i}", "subject_key": f"other.{i}", "content": f"port: {value}",
                  "valid_from": 3000} for i, value in enumerate([5432, 5433] * 4))
    for order in (nodes, list(reversed(nodes))):
        conflicts = analyze_memory_nodes(order)["conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["remedy"]["keep_node"] == "current"
        assert conflicts[0]["remedy"]["retire_node"] == "old"


@pytest.mark.parametrize("contents", [("port: 5433", "port: 5432"),
                                      ("Always deploy changes.", "Never deploy changes.")])
def test_conflict_keeper_uses_effective_time_for_backfilled_facts(contents):
    nodes = [{"id": "current", "subject_key": "service", "content": contents[0],
              "valid_from": 2000, "ingested_at": 2500},
             {"id": "historical", "subject_key": "service", "content": contents[1],
              "valid_from": 1000, "ingested_at": 3000}]
    for order in (nodes, list(reversed(nodes))):
        remedies = [conflict["remedy"] for conflict in analyze_memory_nodes(order)["conflicts"]]
        assert remedies
        assert all(remedy["ordering_basis"] == "valid_from" and remedy["keep_node"] == "current"
                   and remedy["retire_node"] == "historical" for remedy in remedies)


@pytest.mark.parametrize("dates", [(2000, 2000), (None, None), (2000, None), (None, 2000),
                                  ("invalid", 2000), (True, 2000), (float("nan"), 2000),
                                  (float("inf"), 2000), (2000, float("-inf"))])
def test_ambiguous_effective_times_never_emit_retirement_targets(dates):
    for contents in (("port: 5432", "port: 5433"),
                     ("Always deploy changes.", "Never deploy changes.")):
        nodes = [{"id": "a", "subject_key": "db", "content": contents[0],
                  "valid_from": dates[0], "ingested_at": 3000},
                 {"id": "b", "subject_key": "db", "content": contents[1],
                  "valid_from": dates[1], "ingested_at": 4000}]
        for order in (nodes, list(reversed(nodes))):
            report = analyze_memory_nodes(order)
            assert report["conflicts"]
            for conflict in report["conflicts"]:
                remedy = conflict["remedy"]
                assert remedy["action"] == "clarify"
                assert "keep_node" not in remedy
                assert "retire_node" not in remedy
                assert set(remedy["candidate_nodes"]) == {"a", "b"}
            assert all("keep_node" not in remedy and "retire_node" not in remedy
                       for remedy in report["remediation_plan"])


@pytest.mark.parametrize("dates", [(0, -1000), (2000, 0), ("2000", "1000")])
def test_zero_negative_and_numeric_string_effective_dates_are_preserved(dates):
    nodes = [{"id": "current", "subject_key": "db", "content": "port: 5433",
              "valid_from": dates[0], "ingested_at": "invalid"},
             {"id": "historical", "subject_key": "db", "content": "port: 5432",
              "valid_from": dates[1], "ingested_at": float("nan")}]
    report = analyze_memory_nodes(nodes)
    assert report["nodes"][0]["valid_from"] == float(dates[0])
    assert report["nodes"][1]["valid_from"] == float(dates[1])
    assert report["conflicts"][0]["remedy"]["keep_node"] == "current"


def test_unproven_divergence_and_duplicate_suggestions_do_not_choose_a_keeper():
    divergence = analyze_memory_nodes([
        {"id": "a", "subject_key": "service", "claim_kind": "deployment", "content": "Blue deployment is active."},
        {"id": "b", "subject_key": "service", "claim_kind": "deployment", "content": "Manual maintenance procedures are documented."},
    ])
    assert divergence["conflicts"]
    assert divergence["conflicts"][0]["remedy"]["action"] == "clarify"
    assert "keep_node" not in divergence["conflicts"][0]["remedy"]
    assert "retire_node" not in divergence["conflicts"][0]["remedy"]
    duplicates = analyze_memory_nodes([
        {"id": "a", "content": "Run the release verification suite."},
        {"id": "b", "content": "Run the release verification suite."},
    ])
    assert duplicates["redundancies"]
    assert "keep_node" not in duplicates["redundancies"][0]["remedy"]
