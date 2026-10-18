"""Primary memory labels use active prose, matching the policy code boundary."""
import pytest

from engraphis.core.spec_crawl import analyze_memory_nodes


@pytest.mark.parametrize("title,content,axis,hidden_review", [
    ("Note", "A factual note.\n```text\nreview verify check test validate\n```", "context", True),
    ("Note", "A factual note. `review verify check test validate`", "context", True),
    ("Note", "A factual note.\n~~~text\nreview verify check test validate\n~~~", "context", True),
    ("Note", "A factual note.\n```text\nreview verify check test validate", "context", True),
    ("Note", "A factual note. `review\nverify check test validate`", "context", True),
    ("`review verify check test validate`", "A factual note.", "context", True),
    ("```text\nreview verify check test validate", "A literal value.\n```\nA factual note.", "context", True),
    ("Context\n```text\nreview verify check test validate", "A literal value.\n```", "context", True),
    ("Rules", "Never skip checks. `review verify check test validate`", "rules", True),
    ("Role persona", "review verify", "role", False),
    ("Note", "Review verify check test validate", "review", False),
    ("Review", "A factual note.", "review", False),
], ids=[
    "fenced-body", "inline-body", "tilde-fence", "unclosed-fence", "multiline-inline",
    "inline-title", "fence-crosses-title-boundary", "prose-title-before-crossing-fence",
    "active-rules-with-code-noise", "plain-title-weight", "active-review-prose", "active-review-title",
])
@pytest.mark.parametrize("include_trace", [False, True])
def test_primary_axis_uses_visible_policy_prose(title, content, axis, hidden_review, include_trace):
    report = analyze_memory_nodes([
        {"id": "sample", "title": title, "content": content},
    ], include_trace=include_trace)
    node = report["nodes"][0]
    assert node["axis"] == axis
    assert "content" not in node
    if hidden_review:
        assert "review" not in node["covered_axes"]
        assert report["coverage"]["review"] == report["radar"]["review"] == 0
        assert any(gap["axis"] == "review" for gap in report["policy_gaps"])
    visits = [event for event in report["trace"] if event["verb"] == "node_visit"]
    if include_trace:
        assert len(visits) == 1
        assert visits[0]["node_id"] == node["id"]
        assert visits[0]["axis"] == node["axis"] == axis
    else:
        assert report["trace"] == []
