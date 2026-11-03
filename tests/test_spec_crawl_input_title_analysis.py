"""Memory audit input bounds and ambiguity in active title prose are explicit."""
import json

import pytest

from engraphis.core.spec_crawl import MAX_FLAGS, MAX_SPEC_WORDS, analyze_memory_nodes
from scripts import spec_crawl as cli


def _nodes(count):
    return [{"id": f"node{i}", "title": f"Node{i}", "content": f"item{i}"}
            for i in range(count)]


@pytest.mark.parametrize("count", [0, 1, 199, 200, 201, 205])
@pytest.mark.parametrize("include_trace", [False, True])
def test_direct_memory_audit_discloses_input_bound(count, include_trace):
    report = analyze_memory_nodes(_nodes(count), include_trace=include_trace)
    assert report["input_node_count"] == count
    assert report["total_eligible_node_count"] == count
    assert report["count_semantics"] == "provided_input"
    assert report["node_limit"] == 200
    assert report["node_count"] == len(report["nodes"]) == min(count, 200)
    assert report["node_selection_truncated"] is (count > 200)
    assert report["analysis_truncated"] is (count > 200)
    assert report["truncated"] is (count > 200)
    assert {node["id"] for node in report["nodes"]} == {f"node{i}" for i in range(min(count, 200))}
    assert all("content" not in node for node in report["nodes"])
    if not include_trace:
        assert report["trace"] == []


def test_omitted_tail_conflict_is_not_reported_as_a_complete_audit():
    nodes = _nodes(201)
    nodes[0].update(content="Must build.", subject_key="deployment", valid_from=1)
    nodes[200].update(content="Never build.", title="Review verification",
                      subject_key="deployment", valid_from=2)
    prefix = analyze_memory_nodes(nodes[:200], include_trace=False)
    report = analyze_memory_nodes(nodes, include_trace=False)
    assert not report["conflicts"]
    assert report["node_count"] == 200 and report["input_node_count"] == 201
    assert report["node_selection_truncated"] and report["analysis_truncated"]
    for key in ["nodes", "conflicts", "conflicts_total", "coverage", "radar", "policy_gaps",
                "score_breakdown", "cluster_health_score", "flags"]:
        assert report[key] == prefix[key]
    included = analyze_memory_nodes([nodes[0], nodes[-1]], include_trace=False)
    assert any(c["severity"] == "high" for c in included["conflicts"])
    assert report["coverage"]["review"] == 0 < included["coverage"]["review"]


def test_generator_input_has_the_same_disclosure_as_a_sequence():
    report = analyze_memory_nodes(iter(_nodes(201)), include_trace=False)
    assert report["input_node_count"] == 201
    assert report["node_count"] == 200
    assert report["node_selection_truncated"] is report["analysis_truncated"] is True


@pytest.mark.parametrize("as_json", [False, True])
def test_actual_cli_preserves_direct_analyzer_sample_counts(monkeypatch, capsys, as_json):
    report = analyze_memory_nodes(_nodes(201), include_trace=False)
    monkeypatch.setattr(cli, "_audit_workspace_memories", lambda **kwargs: report)
    args = ["--workspace", "sample", "--no-trace"] + (["--json"] if as_json else [])
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert not output.err
    if as_json:
        assert json.loads(output.out) == report
    else:
        assert "200 of 201 eligible nodes" in output.out
        assert "health and findings describe this sample" in output.out


@pytest.mark.parametrize("title,expected", [
    ("Verify everything", ["everything"]),
    ("Review all changes", ["all"]),
    ("Review all 3 changes", []),
    ("Review every 5 minutes", []),
    ("Verify `everything`", []),
    ("`Verify everything quickly`", []),
    ("```text\nVerify everything quickly\n```", []),
    ("~~~text\nVerify everything quickly\n~~~", []),
    ("Review all 3 changes and everything", ["everything"]),
    ("Verify everything `quickly` and all", ["everything", "all"]),
    ("Verify everything. Then verify everything.", ["everything", "everything"]),
    ("Verify quickly within 5 seconds", []),
])
@pytest.mark.parametrize("include_trace", [False, True])
def test_masked_title_ambiguity_affects_flags_quality_and_trace(title, expected, include_trace):
    report = analyze_memory_nodes([
        {"id": "memory", "title": title, "content": "A factual note."},
    ], include_trace=include_trace)
    node = report["nodes"][0]
    assert [flag["token"] for flag in report["flags"]] == expected
    assert node["flag_count"] == len(expected)
    assert report["flags_total"] == len(expected)
    assert report["flag_limit"] == MAX_FLAGS
    assert report["flags_truncated"] is False
    assert report["score_breakdown"]["quality_points"] == (0 if expected else 30)
    assert report["score_breakdown"]["vague_penalty"] == -min(15, 2 * len(expected))
    starts = []
    for flag in report["flags"]:
        assert flag["field"] == "title"
        assert flag["id"] == f"memory_vague_title_{flag['start']}"
        assert flag["node_id"] == "memory"
        assert title[flag["start"]:flag["end"]] == flag["token"]
        assert "title" in flag["question"]
        starts.append(flag["start"])
    assert starts == sorted(set(starts))
    if include_trace:
        classification = next(t for t in report["trace"] if t["verb"] == "node_classify")
        assert classification["kinds"].get("vague", 0) == len(expected)
    else:
        assert report["trace"] == []


def test_title_qualifiers_cannot_cross_the_content_field_boundary():
    report = analyze_memory_nodes([
        {"id": "first", "title": "Review all", "content": "3 items."},
        {"id": "second", "title": "Review all 3 items", "content": "Review all items."},
    ], include_trace=False)
    assert [(f["node_id"], f.get("field"), f["token"]) for f in report["flags"]] == [
        ("second", None, "all"), ("first", "title", "all"),
    ]


def test_fence_opened_in_title_does_not_create_title_ambiguity():
    report = analyze_memory_nodes([
        {"id": "fenced", "title": "```text\nVerify everything",
         "content": "Literal value\n```\nA factual note."},
    ])
    assert report["flags"] == []
    assert report["nodes"][0]["flag_count"] == 0
    assert report["nodes"][0]["axis"] == "context"
    assert report["score_breakdown"]["quality_points"] == 30


def test_old_content_flag_records_and_offsets_precede_new_title_flags():
    title = "Verify everything"
    content = "Check everything and all changes."
    report = analyze_memory_nodes([{"id": "stable", "title": title, "content": content}])
    expected = [{"id": f"stable_vague_{content.index(token)}", "node_id": "stable", "token": token,
                 "question": f"In memory '{title}': \"{token}\" is unbounded. What exact number, scope, or condition do you mean?"}
                for token in ["everything", "all"]]
    assert report["flags"][:2] == expected
    assert report["flags"][2]["id"] == "stable_vague_title_7"
    assert report["nodes"][0]["word_count"] == 5
    assert report["nodes"][0]["title_word_count"] == 2
    assert report["nodes"][0]["flag_count"] == 3
    assert report["nodes"][0]["token_kinds"]["vague"] == 3


def test_title_previews_cannot_displace_existing_content_flags_at_the_cap():
    nodes = [{"id": f"n{i}", "title": "Verify everything", "content": "Check everything."}
             for i in range(MAX_FLAGS)]
    report = analyze_memory_nodes(nodes, include_trace=False)
    assert len(report["flags"]) == MAX_FLAGS
    assert [f["id"] for f in report["flags"]] == [f"n{i}_vague_6" for i in range(MAX_FLAGS)]
    assert all("field" not in flag for flag in report["flags"])
    assert all(node["flag_count"] == 2 for node in report["nodes"])
    assert report["flags_total"] == 2 * MAX_FLAGS
    assert report["flags_truncated"] is report["analysis_truncated"] is True
    assert report["score_breakdown"]["quality_points"] == 0


def test_title_only_flags_retain_the_existing_global_display_bound():
    report = analyze_memory_nodes([{
        "id": "many", "title": " ".join(["everything"] * (MAX_FLAGS + 1)), "content": "A factual note.",
    }], include_trace=False)
    assert len(report["flags"]) == MAX_FLAGS
    assert report["flags_total"] == MAX_FLAGS + 1
    assert report["nodes"][0]["flag_count"] == MAX_FLAGS + 1
    assert report["flags_truncated"] is report["analysis_truncated"] is True


@pytest.mark.parametrize("count", [MAX_SPEC_WORDS, MAX_SPEC_WORDS + 1])
def test_long_title_word_bounds_are_disclosed_without_changing_body_offsets(count):
    report = analyze_memory_nodes([{
        "id": "long", "title": " ".join(["neutral"] * count), "content": "A factual note.",
    }], include_trace=False)
    node = report["nodes"][0]
    partial = count > MAX_SPEC_WORDS
    assert node["word_count"] == 3
    assert node["title_word_count"] == MAX_SPEC_WORDS
    assert node["title_word_count_is_lower_bound"] is partial
    assert node["title_word_analysis_truncated"] is partial
    assert node["word_analysis_truncated"] is partial
    assert report["word_analysis_truncated_node_count"] == int(partial)
    assert report["analysis_truncated"] is partial
    assert report["score_breakdown"]["quality_points"] == (0 if partial else 30)


def test_title_flag_ids_cannot_collide_with_another_nodes_content_id():
    report = analyze_memory_nodes([
        {"id": "a", "title": "Verify everything", "content": "A factual note."},
        {"id": "a_title", "title": "Note", "content": "Review everything."},
    ], include_trace=False)
    ids = [flag["id"] for flag in report["flags"]]
    assert len(ids) == len(set(ids)) == 2
    assert ids == ["a_title_vague_7", "a_vague_title_7"]
    assert report["flags"][0]["node_id"] == "a_title"
    assert report["flags"][1]["node_id"] == "a"
    assert report["flags"][1]["field"] == "title"
