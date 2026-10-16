"""Unit tests for multi-node memory analysis in engraphis.core.spec_crawl."""
from __future__ import annotations

import pytest

from engraphis.core.spec_crawl import analyze_memory_nodes


@pytest.mark.parametrize("title,uncovered_axis", [
    ("Preview configuration", "review"),
    ("Rolex display", "role"),
    ("Reagent inventory", "team"),
])
def test_axis_aliases_and_signals_require_whole_words(title, uncovered_axis):
    report = analyze_memory_nodes([
        {"id": "unrelated", "title": title, "content": "Raster output dimensions."},
    ])
    assert report["coverage"][uncovered_axis] == 0
    assert any(gap["axis"] == uncovered_axis for gap in report["policy_gaps"])


@pytest.mark.parametrize("title,axis", [
    ("Review: deployment", "review"),
    ("Roles & owners", "team"),
    ("Non-negotiable boundaries", "rules"),
])
def test_whole_word_title_matching_preserves_real_aliases(title, axis):
    report = analyze_memory_nodes([
        {"id": "policy", "title": title, "content": "Raster output dimensions."},
    ])
    assert report["nodes"][0]["axis"] == axis


@pytest.mark.parametrize("word,axis", [
    ("Preview", "review"), ("Reagent", "team"), ("Firstly", "start"),
])
def test_single_document_body_signals_also_require_whole_words(word, axis):
    from engraphis.core.spec_crawl import crawl_spec

    report = crawl_spec(f"# Context\n{word} dimensions.")
    assert report["coverage"][axis] == 0


def test_large_memory_similarity_is_bounded_cached_and_disclosed():
    from unittest.mock import patch

    from engraphis.core import spec_crawl

    token_limit = spec_crawl.MAX_MEMORY_SIMILARITY_TOKENS
    content = " ".join(f"shared{i}" for i in range(token_limit + 100))
    nodes = [{"id": f"node-{i}", "content": f"{content} distinct{i}",
              "subject_key": "shared", "claim_kind": "status"} for i in range(8)]
    calls = []
    original_jaccard = spec_crawl.jaccard

    def checked_jaccard(left, right):
        assert len(left) <= token_limit
        assert len(right) <= token_limit
        calls.append((len(left), len(right)))
        return original_jaccard(left, right)

    with patch.object(spec_crawl, "jaccard", side_effect=checked_jaccard):
        report = analyze_memory_nodes(nodes)
    assert 0 < len(calls) <= len(nodes) * (len(nodes) - 1) // 2
    assert report["similarity_token_limit"] == token_limit
    assert report["similarity_sampled_node_count"] == len(nodes)
    assert all(node["similarity_sampled"] for node in report["nodes"])
    assert all(node["similarity_token_count"] == token_limit for node in report["nodes"])
    assert report == analyze_memory_nodes(nodes)


def test_similarity_sampling_preserves_full_text_policy_conflicts():
    from engraphis.core.spec_crawl import MAX_MEMORY_SIMILARITY_TOKENS

    prefix = " ".join(f"neutral{i}" for i in range(MAX_MEMORY_SIMILARITY_TOKENS * 2))
    nodes = [
        {"id": "old", "subject_key": "database", "valid_from": 1,
         "content": f"{prefix}\nport: 5432. Always require authentication."},
        {"id": "new", "subject_key": "database", "valid_from": 2,
         "content": f"{prefix}\nport: 5433. Never require authentication."},
    ]
    report = analyze_memory_nodes(nodes)
    assert report["similarity_sampled_node_count"] == 2
    assert report["conflicts"]
    assert report["conflicts"][0]["remedy"]["keep_node"] == "new"
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"
    assert report["nodes"][0]["parameters"]["port"] == "5432"
    assert report["nodes"][1]["directives"]["authentication"] == "forbid"


def test_small_memory_similarity_remains_exact_and_unsampled():
    from engraphis.core.textutil import jaccard, tokenize

    nodes = [{"id": "a", "title": "Authentication", "content": "Verify token authentication."},
             {"id": "b", "title": "Authentication", "content": "Verify token authentication before deployment."}]
    report = analyze_memory_nodes(nodes)
    expected = jaccard(*(tokenize(f"{node['title']} {node['content']}") for node in nodes))
    assert report["similarity_sampled_node_count"] == 0
    assert not any(node["similarity_sampled"] for node in report["nodes"])
    assert report["links"][0]["similarity"] == round(expected, 2)


@pytest.mark.parametrize("key", ["port", "timeout", "retries", "max_retries"])
@pytest.mark.parametrize("value", ["5432", "1.5", "-1"])
def test_numeric_parameter_sentence_punctuation_does_not_create_a_conflict(key, value):
    report = analyze_memory_nodes([
        {"id": "sentence", "subject_key": "config", "content": f"{key}: {value}."},
        {"id": "line", "subject_key": "config", "content": f"{key}: {value}"},
    ])
    assert not report["conflicts"]
    assert all(node["parameters"][key] == value for node in report["nodes"])


def test_numeric_parameter_normalization_preserves_dotted_identifiers():
    from engraphis.core.spec_crawl import _extract_node_parameters

    assert _extract_node_parameters("host: db.example.com. version: 1.2.3.") == {
        "host": "db.example.com.", "version": "1.2.3.",
    }


def test_unrelated_policies_do_not_hide_an_earlier_directive_conflict():
    policies = [
        {"id": "require", "subject_key": "prod", "content": "Always require authentication."},
        {"id": "forbid", "subject_key": "prod", "content": "Never require authentication."},
    ]
    noise = [{"id": f"noise-{i}", "subject_key": f"noise.{i}",
              "content": "Always require authentication."} for i in range(4)]
    for order in (policies, policies + noise, list(reversed(policies + noise))):
        conflicts = analyze_memory_nodes(order)["conflicts"]
        assert len(conflicts) == 1
        assert {conflicts[0]["node_a"], conflicts[0]["node_b"]} == {"require", "forbid"}


@pytest.mark.parametrize("rule,conflicts", [
    ("Never disable authentication.", False),
    ("Must not disable authentication.", False),
    ("Do not skip authentication.", False),
    ("Never prohibit authentication.", False),
    ("Always disable authentication.", True),
    ("Never require authentication.", True),
])
def test_nested_policy_polarity_does_not_suggest_retiring_agreeing_rules(rule, conflicts):
    nodes = [
        {"id": "first", "subject_key": "prod", "content": rule, "valid_from": 1000},
        {"id": "second", "subject_key": "prod", "content": "Always require authentication.",
         "valid_from": 2000},
    ]
    for order in (nodes, list(reversed(nodes))):
        report = analyze_memory_nodes(order)
        assert bool(report["conflicts"]) == conflicts
        if not conflicts:
            assert all(remedy["action"] != "supersede" for remedy in report["remediation_plan"])


def test_analyze_empty_memory_nodes():
    res = analyze_memory_nodes([])
    assert res["cluster_health_score"] == 0
    assert res["node_count"] == 0
    assert len(res["policy_gaps"]) == 7
    assert len(res["conflicts"]) == 0


def test_cluster_responses_omit_large_memory_bodies_without_losing_analysis():
    import json
    report = analyze_memory_nodes([{"id": "large", "content": "payload " * 12_500}])
    assert "content" not in report["nodes"][0]
    assert report["nodes"][0]["word_count"] > 0
    assert len(json.dumps(report)) < 6_000


def test_memory_word_truncation_is_disclosed_and_cannot_count_as_clean():
    from engraphis.core.spec_crawl import MAX_SPEC_WORDS

    report = analyze_memory_nodes([
        {"id": "long", "content": "neutral " * MAX_SPEC_WORDS + "every result."},
    ])
    node = report["nodes"][0]
    assert node["word_count"] == MAX_SPEC_WORDS
    assert node["word_analysis_truncated"] is True
    assert node["word_count_is_lower_bound"] is True
    assert node["flag_count"] == 0
    assert report["word_limit"] == MAX_SPEC_WORDS
    assert report["word_analysis_truncated_node_count"] == 1
    assert report["analysis_truncated"] is True
    assert report["score_breakdown"]["quality_points"] == 0


def test_exact_word_limit_is_complete_and_only_complete_clean_nodes_earn_quality():
    from engraphis.core.spec_crawl import MAX_SPEC_WORDS

    nodes = [{"id": "exact", "content": "neutral " * MAX_SPEC_WORDS},
             {"id": "partial", "content": "neutral " * (MAX_SPEC_WORDS + 1)}]
    report = analyze_memory_nodes(nodes)
    complete, partial = report["nodes"]
    assert complete["word_analysis_truncated"] is False
    assert complete["word_count_is_lower_bound"] is False
    assert partial["word_analysis_truncated"] is True
    assert report["word_analysis_truncated_node_count"] == 1
    assert report["score_breakdown"]["quality_points"] == 15
    exact = analyze_memory_nodes(nodes[:1])
    assert exact["analysis_truncated"] is False
    assert exact["word_analysis_truncated_node_count"] == 0
    assert exact["score_breakdown"]["quality_points"] == 30


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


@pytest.mark.parametrize("code_style", ["fenced", "inline"])
def test_configuration_examples_do_not_create_supersession_remedies(code_style):
    example = "auth: none. Never require authentication."
    example = f"```text\n{example}\n```" if code_style == "fenced" else f"`{example}`"
    report = analyze_memory_nodes([
        {"id": "guidance", "subject_key": "app", "valid_from": 2,
         "content": f"Use OAuth.\nObsolete example:\n{example}"},
        {"id": "current", "subject_key": "app", "valid_from": 1,
         "content": "auth: oauth. Always require authentication."},
    ])
    assert report["nodes"][0]["parameters"] == {}
    assert report["nodes"][0]["directives"] == {}
    assert not report["conflicts"]
    assert all(remedy["action"] != "supersede" for remedy in report["remediation_plan"])


@pytest.mark.parametrize("code_style", ["fenced", "inline"])
def test_active_conflicts_survive_agreeing_code_examples(code_style):
    example = "port: 9999. Never require authentication."
    example = f"```text\n{example}\n```" if code_style == "fenced" else f"`{example}`"
    report = analyze_memory_nodes([
        {"id": "old", "subject_key": "app", "valid_from": 1,
         "content": f"port: 5432. Always require authentication.\n{example}"},
        {"id": "new", "subject_key": "app", "valid_from": 2,
         "content": f"port: 5433. Never require authentication.\n{example}"},
    ])
    old, new = report["nodes"]
    assert old["parameters"]["port"] == "5432"
    assert new["parameters"]["port"] == "5433"
    assert old["directives"]["authentication"] == "enforce"
    assert new["directives"]["authentication"] == "forbid"
    assert report["conflicts"][0]["remedy"]["keep_node"] == "new"
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"


def test_removed_inline_examples_cannot_join_parameter_or_directive_fragments():
    report = analyze_memory_nodes([
        {"id": "example", "content": "auth: `none`\nKeep OAuth enabled.\nAlways `require` authentication."},
    ])
    assert report["nodes"][0]["parameters"] == {}
    assert report["nodes"][0]["directives"] == {}


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


def test_similarity_suggestions_do_not_count_as_persisted_connectivity():
    nodes = [{"id": "a", "content": "Verify token authentication for deployment."},
             {"id": "b", "content": "Verify token authentication before deployment."}]
    suggested = analyze_memory_nodes(nodes)
    assert any(link["inferred"] for link in suggested["links"])
    assert all(node["is_orphan"] and node["degree"] == 0 for node in suggested["nodes"])
    assert {orphan["node_id"] for orphan in suggested["orphans"]} == {"a", "b"}
    assert any(remedy["action"] == "link" for remedy in suggested["remediation_plan"])
    assert suggested["score_breakdown"]["connected_points"] == 0
    declared = analyze_memory_nodes(nodes, links=[{"a": "a", "b": "b", "relation": "related_to"}])
    assert not declared["orphans"]
    assert all(node["degree"] == 1 for node in declared["nodes"])
    assert declared["score_breakdown"]["connected_points"] == 30


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


HANDBOOK = """# Role
You are the backend maintainer.
# Objective
Deliver one audited release.
# Context
The codebase uses the local datastore architecture.
# Team
The owner assigns the reviewer and delegates tasks.
# Rules
Never deploy without explicit approval.
# Review
Verify the unit tests and release checks.
# Start
Begin with the setup commands.
"""


@pytest.mark.parametrize("copies", [1, 2])
def test_each_handbook_memory_covers_all_explicit_nonempty_policy_sections(copies):
    from engraphis.core.spec_crawl import DEFAULT_AXES

    report = analyze_memory_nodes([
        {"id": f"handbook-{i}", "title": "Handbook", "content": HANDBOOK}
        for i in range(copies)
    ])
    assert all(node["axis"] == "team" for node in report["nodes"])
    assert all(node["covered_axes"] == list(DEFAULT_AXES) for node in report["nodes"])
    assert report["coverage"] == {axis: copies / 2 for axis in DEFAULT_AXES}
    assert report["policy_gaps"] == []


def test_repeated_sections_count_each_axis_only_once_per_memory():
    report = analyze_memory_nodes([{
        "id": "review", "title": "Release review", "content":
        "# Review\nVerify the release checks.\n# Review\nRun the unit tests.",
    }])
    assert report["nodes"][0]["covered_axes"].count("review") == 1
    assert report["coverage"]["review"] == 0.5


def test_code_only_and_empty_policy_sections_cannot_remove_memory_policy_gaps():
    report = analyze_memory_nodes([{
        "id": "example", "title": "Handbook example", "content": f"```markdown\n{HANDBOOK}```",
    }])
    assert report["nodes"][0]["covered_axes"] == []
    assert all(value == 0 for value in report["coverage"].values())
    active = analyze_memory_nodes([{
        "id": "context", "title": "Reference", "content":
        "# Review\n`Verify tests then deploy.`\n# Context\nSQLite stores repository facts.",
    }])
    assert active["coverage"]["context"] > 0
    assert active["coverage"]["review"] == 0


def test_multiple_clear_topics_in_one_memory_preserve_the_primary_axis_field():
    report = analyze_memory_nodes([{
        "id": "policy", "title": "Role and Objective", "content":
        "You are the backend maintainer. The goal is to deliver one release.",
    }])
    assert isinstance(report["nodes"][0]["axis"], str)
    assert {"role", "objective"} <= set(report["nodes"][0]["covered_axes"])
    assert report["coverage"]["role"] == report["coverage"]["objective"] == 0.5


@pytest.mark.parametrize("title", ["~~~", "Introduction\n~~~", "`port: 9001`"])
def test_policy_mask_reuse_preserves_title_fences_and_inline_examples(title):
    body = "port: 9002" if title.startswith("`") else "port: 9001\n~~~\nport: 9002"
    report = analyze_memory_nodes([
        {"id": "a", "subject_key": "service", "title": title, "content": body},
        {"id": "b", "subject_key": "service", "content": "port: 9002"},
    ])
    assert not report["conflicts"]


@pytest.mark.parametrize("title", ["~~~", "Introduction\n~~~", "`Never deploy changes.`"])
def test_policy_mask_reuse_keeps_example_directives_out_of_conflicts(title):
    body = "Always deploy changes." if title.startswith("`") else (
        "Never deploy changes.\n~~~\nAlways deploy changes.")
    report = analyze_memory_nodes([
        {"id": "a", "subject_key": "service", "title": title, "content": body},
        {"id": "b", "subject_key": "service", "content": "Always deploy changes."},
    ])
    assert not report["conflicts"]


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


@pytest.mark.parametrize("verb", ["require", "enforce"])
@pytest.mark.parametrize("negative", ["Never", "Must not", "Do not"])
def test_nested_directives_preserve_governing_negation(verb, negative):
    nodes = [{"id": "a", "content": f"{negative} {verb} token verification."},
             {"id": "b", "content": f"Always {verb} token verification."}]
    for order in (nodes, list(reversed(nodes))):
        report = analyze_memory_nodes(order)
        assert report["conflicts"]
        assert all(conflict["remedy"]["action"] == "clarify" for conflict in report["conflicts"])
    equivalent = analyze_memory_nodes([
        nodes[0], {"id": "c", "content": "Never token verification."},
    ])
    assert not equivalent["conflicts"]


@pytest.mark.parametrize("separator", [". ", "; ", ", ", "\n", " and always "])
def test_independent_directives_do_not_inherit_prior_negation(separator):
    from engraphis.core.spec_crawl import _extract_node_directives
    directives = _extract_node_directives(f"Never deploy{separator}require authentication.")
    assert directives["deploy"] == "forbid"
    assert directives["authentication"] == "enforce"


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
