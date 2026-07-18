import json
import io
import os
from types import SimpleNamespace

import pytest

from scripts import graph_cli
from scripts.graph_cli import _merge_payloads, build_parser


def test_console_json_survives_windows_charmap_stdout(monkeypatch):
    sink = io.BytesIO()
    stream = io.TextIOWrapper(sink, encoding="cp1252")
    monkeypatch.setattr(graph_cli.sys, "stdout", stream)

    graph_cli._json({"edge": "caller → callee"})
    stream.flush()

    assert b"caller \\u2192 callee" in sink.getvalue()


def test_help_hides_internal_merge_and_describes_installer(capsys):
    with pytest.raises(SystemExit) as exc:
        graph_cli.build_parser().parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "==SUPPRESS==" not in out
    assert "Install the graph union merge driver" in out


def test_graph_cli_exposes_repo_workflow_commands():
    parser = build_parser()
    for command in (
        "index", "search", "query", "explain", "path", "impact", "prs", "export",
        "postgres",
    ):
        args = parser.parse_args(
            [command, "--workspace", "w", "--repo", "r"]
            + (
                ["--root", "."] if command == "index"
                else ["query"] if command in {"search", "query", "explain"}
                else ["a", "b"] if command == "path"
                else []
            )
        )
        assert args.command == command


def test_git_files_rejects_option_lookalike_revisions(tmp_path):
    """The revision sits before the `--` pathspec separator, so a leading-dash
    value would be parsed by git as an option — e.g. `--output=<file>` is an
    arbitrary-file-write primitive. `impact --git-range` and `prs --base/--head`
    accept free-form strings, so this must fail closed (PR #19 review follow-up)."""
    from engraphis.service import ValidationError

    for hostile in ("--output=owned.txt", "-p", "--no-index"):
        with pytest.raises(ValidationError, match="invalid git revision"):
            graph_cli._git_files(str(tmp_path), hostile)
    assert not (tmp_path / "owned.txt").exists()


def test_graph_union_merge_deduplicates_symbols_and_remaps_memory_links():
    base = {
        "format": "engraphis-code-graph/1",
        "generated_at": 1,
        "nodes": [{"id": "old", "file": "a.py", "fqname": "run",
                   "name": "run", "kind": "function", "updated_at": 1}],
        "files": [{"file": "a.py"}],
        "edges": [{
            "src": "old", "dst": "external", "relation": "calls",
            "file": "a.py", "line": 1,
        }],
        "memory_links": [{"repo_id": "r", "symbol_id": "old",
                          "memory_id": "m", "relation": "mentions"}],
    }
    other = {
        **base,
        "generated_at": 2,
        "nodes": [{"id": "new", "file": "a.py", "fqname": "run",
                   "name": "run", "kind": "function", "updated_at": 2}],
        "memory_links": [{"repo_id": "r", "symbol_id": "new",
                          "memory_id": "m", "relation": "mentions"}],
    }
    merged = _merge_payloads([base, other])
    assert [node["id"] for node in merged["nodes"]] == ["new"]
    assert merged["memory_links"][0]["symbol_id"] == "new"
    assert merged["edges"][0]["src"] == "new"
    assert _merge_payloads([other, base]) == merged


def test_graph_union_merge_tolerates_invalid_timestamps():
    merged = _merge_payloads([
        {"generated_at": "invalid", "nodes": [], "files": [], "edges": []},
        {"generated_at": 2, "nodes": [], "files": [], "edges": []},
    ])
    assert merged["generated_at"] == 2


def test_graph_union_merge_ignores_malformed_rows_and_normalizes_line_numbers():
    merged = _merge_payloads([{
        "format": "engraphis-code-graph/1",
        "generated_at": "not-finite",
        "files": "not-a-list",
        "nodes": [
            None,
            "bad",
            {"id": "sym", "file": "a.py", "fqname": "run", "kind": "function"},
        ],
        "edges": [
            {"src": "", "dst": "run", "relation": "calls"},
            {"src": "run", "dst": "helper", "relation": {"bad": True}, "line": "bad"},
            {"src": "run", "dst": "helper", "relation": "calls", "layer": "causal"},
            {"src": "run", "dst": "helper", "relation": "calls", "layer": "semantic"},
        ],
        "memory_links": [
            {"symbol_id": "", "memory_id": "mem_x"},
            {"symbol_id": "sym", "memory_id": "mem_x", "relation": "mentions"},
        ],
        "analysis": "not-an-object",
    }])

    assert [node["id"] for node in merged["nodes"]] == ["sym"]
    assert merged["edges"][0]["line"] == 0
    assert {edge["layer"] for edge in merged["edges"] if edge["relation"] == "calls"} == {
        "causal", "semantic",
    }
    assert len(merged["memory_links"]) == 1
    assert merged["analysis"] == {}
    json.dumps(merged, allow_nan=False)


def test_merge_driver_rejects_oversized_or_deep_inputs(monkeypatch, tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_text("x" * 20, encoding="utf-8")
    monkeypatch.setattr(graph_cli, "MAX_MERGE_EXPORT_BYTES", 10)
    assert graph_cli._load_merge_payload(str(oversized)) is None

    deep = tmp_path / "deep.json"
    deep.write_text("[" * 205 + "0" + "]" * 205, encoding="utf-8")
    monkeypatch.setattr(graph_cli, "MAX_MERGE_EXPORT_BYTES", 10_000)
    assert graph_cli._load_merge_payload(str(deep)) is None


def test_merge_driver_writes_valid_output_when_one_parent_is_malformed(tmp_path):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    base.write_text("{bad json", encoding="utf-8")
    current.write_text(json.dumps({
        "format": "engraphis-code-graph/1",
        "generated_at": 1,
        "nodes": [],
        "files": [],
        "edges": [],
        "memory_links": [],
    }), encoding="utf-8")
    other.write_text(json.dumps({
        "format": "engraphis-code-graph/1",
        "generated_at": 2,
        "nodes": [],
        "files": [],
        "edges": [],
        "memory_links": [],
    }), encoding="utf-8")

    graph_cli._merge(SimpleNamespace(
        base=str(base), current=str(current), other=str(other)
    ))

    merged = json.loads(current.read_text(encoding="utf-8"))
    assert merged["format"] == "engraphis-code-graph/1"
    assert not current.with_name(current.name + ".engraphis.tmp").exists()


def test_merge_driver_fails_closed_when_current_or_incoming_is_invalid(tmp_path):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    base.write_text("{}", encoding="utf-8")
    current.write_text('{"format":"engraphis-code-graph/1"}', encoding="utf-8")
    other.write_text("{bad json", encoding="utf-8")
    before = current.read_bytes()

    with pytest.raises(graph_cli.ValidationError, match="current and incoming"):
        graph_cli._merge(SimpleNamespace(
            base=str(base), current=str(current), other=str(other)
        ))

    assert current.read_bytes() == before


def test_merge_driver_rejects_unknown_graph_format(tmp_path):
    source = tmp_path / "graph.json"
    source.write_text('{"format":"hostile/1"}', encoding="utf-8")
    assert graph_cli._load_merge_payload(str(source)) is None


def test_merge_driver_does_not_use_predictable_temp_symlink(tmp_path):
    payload = json.dumps({
        "format": "engraphis-code-graph/1",
        "generated_at": 1,
        "nodes": [],
        "files": [],
        "edges": [],
        "memory_links": [],
    })
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    for path in (base, current, other):
        path.write_text(payload, encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    predictable = current.with_name(current.name + ".engraphis.tmp")
    try:
        os.symlink(outside, predictable)
    except (OSError, NotImplementedError):
        predictable.write_text("sentinel", encoding="utf-8")

    graph_cli._merge(SimpleNamespace(
        base=str(base), current=str(current), other=str(other)
    ))

    assert outside.read_text(encoding="utf-8") == "untouched"
    assert predictable.exists()


def test_install_merge_driver_rejects_unsafe_attributes_before_git(monkeypatch, tmp_path):
    (tmp_path / ".gitattributes").mkdir()
    monkeypatch.setattr(
        graph_cli.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git config ran before path validation"),
    )

    with pytest.raises(graph_cli.ValidationError, match="regular file"):
        graph_cli._install_merge_driver(SimpleNamespace(
            root=str(tmp_path), graph_path="graph.json"
        ))
