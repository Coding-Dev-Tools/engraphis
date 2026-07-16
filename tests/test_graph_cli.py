from scripts.graph_cli import _merge_payloads, build_parser


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
