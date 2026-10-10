"""Unit tests for multi-node memory analysis in engraphis.core.spec_crawl."""
from __future__ import annotations

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
        },
        {
            "id": "mem_db_v2",
            "title": "Updated Database Config",
            "content": "The main database is set to sqlite. Port is 5432.",
            "mtype": "semantic",
            "subject_key": "db.config",
            "ingested_at": 2000.0,
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
