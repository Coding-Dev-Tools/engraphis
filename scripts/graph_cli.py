"""Repository-graph CLI over the same v2 MemoryService used by MCP and the dashboard."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from engraphis.config import settings
from engraphis.service import MemoryService, ValidationError


def _service() -> MemoryService:
    return MemoryService.create(
        settings.db_path,
        embed_model=settings.embed_model or None,
        allowed_workspaces=settings.allowed_workspaces,
        extractor=settings.extractor,
    )


def _json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _git_files(root: str, revision: str) -> list[str]:
    cmd = ["git", "-C", str(Path(root).resolve()), "diff", "--name-only"]
    if revision:
        cmd.append(revision)
    cmd.append("--")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValidationError(result.stderr.strip() or "git diff failed")
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines()
            if line.strip()]


def _index(args) -> None:
    _json(_service().index_repo(
        workspace=args.workspace, repo=args.repo, root_path=args.root,
        languages=args.languages,
    ))


def _search(args) -> None:
    _json(_service().search_code(
        args.query, workspace=args.workspace, repo=args.repo, limit=args.limit,
    ))


def _query(args) -> None:
    _json(_service().intent_recall(
        args.query, intent=args.intent, workspace=args.workspace, repo=args.repo,
        k=args.limit, reinforce=False,
    ))


def _path(args) -> None:
    _json(_service().code_path(
        args.source, args.target, workspace=args.workspace, repo=args.repo,
        max_depth=args.max_depth,
    ))


def _impact(args) -> None:
    files = list(args.files or [])
    if args.git_range is not None:
        files.extend(_git_files(args.root, args.git_range))
    if not files:
        files = _git_files(args.root, "")
    _json(_service().code_impact(files, workspace=args.workspace, repo=args.repo))


def _prs(args) -> None:
    current_files = _git_files(args.root, f"{args.base}...{args.head}")
    current = _service().code_impact(
        current_files, workspace=args.workspace, repo=args.repo,
    )
    if not args.conflicts_with:
        _json({"mode": "triage", "range": f"{args.base}...{args.head}", **current})
        return
    other_files = _git_files(args.root, f"{args.base}...{args.conflicts_with}")
    other = _service().code_impact(
        other_files, workspace=args.workspace, repo=args.repo,
    )
    overlap_files = sorted(
        set(current["changed_files"]) & set(other["changed_files"])
        | set(current["dependent_files"]) & set(other["dependent_files"])
    )
    overlap_communities = sorted(
        set(current["communities_affected"]) & set(other["communities_affected"])
    )
    zones = sorted(
        set(current["potential_conflict_zones"])
        & set(other["potential_conflict_zones"])
    )
    _json({
        "mode": "conflicts",
        "current": current,
        "other": other,
        "overlap": {
            "files": overlap_files,
            "communities": overlap_communities,
            "hotspots": zones,
            "risk": "high" if zones or overlap_communities else
                    "medium" if overlap_files else "low",
        },
    })


def _export(args) -> None:
    result = _service().export_code_graph(workspace=args.workspace, repo=args.repo)
    out = Path(args.output).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(
        json.dumps(result["graph"], indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    (out / "GRAPH_REPORT.md").write_text(result["report_markdown"], encoding="utf-8")
    (out / "graph.html").write_text(result["graph_html"], encoding="utf-8")
    print(f"Exported graph.json, graph.html, and GRAPH_REPORT.md to {out}")


def _postgres(args) -> None:
    dsn = os.environ.get(args.dsn_env, "")
    if not dsn:
        raise ValidationError(f"{args.dsn_env} is not set")
    _json(_service().import_postgres_schema(
        dsn, workspace=args.workspace, repo=args.repo, schemas=args.schemas,
        actor="cli",
    ))


def _natural_node(node: dict) -> tuple:
    return (
        str(node.get("file") or ""), str(node.get("fqname") or node.get("name") or ""),
        str(node.get("kind") or ""),
    )


def _stable_json(value: dict) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _timestamp(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _candidate_key(value: dict, parent_timestamp: float = 0.0) -> tuple[float, str]:
    updated_at = _timestamp(value.get("updated_at") or parent_timestamp)
    return updated_at, _stable_json(value)


def _merge_payloads(payloads: list[dict]) -> dict:
    payloads = [payload for payload in payloads if isinstance(payload, dict)]
    if not payloads:
        return {"format": "engraphis-code-graph/1", "files": [], "nodes": [],
                "edges": [], "memory_links": [], "analysis": {}}
    files = {}
    nodes = {}
    old_to_new = {}
    for payload in payloads:
        generated_at = _timestamp(payload.get("generated_at"))
        for item in payload.get("files") or []:
            key = str(item.get("file") or "")
            chosen = files.get(key)
            if chosen is None or _candidate_key(item, generated_at) > chosen[0]:
                files[key] = (_candidate_key(item, generated_at), item)
        for node in payload.get("nodes") or []:
            key = _natural_node(node)
            chosen = nodes.get(key)
            if chosen is None or _candidate_key(node, generated_at) > chosen[0]:
                nodes[key] = (_candidate_key(node, generated_at), node)
    for payload in payloads:
        for node in payload.get("nodes") or []:
            chosen_entry = nodes.get(_natural_node(node))
            chosen = chosen_entry[1] if chosen_entry else None
            if node.get("id") and chosen and chosen.get("id"):
                old_to_new[node["id"]] = chosen["id"]
    edges = {}
    links = {}
    for payload in payloads:
        generated_at = _timestamp(payload.get("generated_at"))
        for edge in payload.get("edges") or []:
            item = dict(edge)
            item["src"] = old_to_new.get(item.get("src"), item.get("src"))
            item["dst"] = old_to_new.get(item.get("dst"), item.get("dst"))
            key = (
                item.get("src"), item.get("dst"), item.get("relation"),
                item.get("file"), item.get("line"),
            )
            chosen = edges.get(key)
            if chosen is None or _candidate_key(item, generated_at) > chosen[0]:
                edges[key] = (_candidate_key(item, generated_at), item)
        for link in payload.get("memory_links") or []:
            item = dict(link)
            item["symbol_id"] = old_to_new.get(item.get("symbol_id"), item.get("symbol_id"))
            key = (
                item.get("repo_id"), item.get("symbol_id"),
                item.get("memory_id"), item.get("relation"),
            )
            chosen = links.get(key)
            if chosen is None or _candidate_key(item, generated_at) > chosen[0]:
                links[key] = (_candidate_key(item, generated_at), item)
    latest = max(
        payloads,
        key=lambda payload: (
            _timestamp(payload.get("generated_at")),
            _stable_json(payload),
        ),
    )
    return {
        "format": "engraphis-code-graph/1",
        "generated_at": latest.get("generated_at"),
        "repo_id": latest.get("repo_id"),
        "files": sorted(
            (entry[1] for entry in files.values()),
            key=lambda item: str(item.get("file") or ""),
        ),
        "nodes": sorted((entry[1] for entry in nodes.values()), key=_natural_node),
        "edges": sorted(
            (entry[1] for entry in edges.values()),
            key=lambda item: (
                str(item.get("src") or ""), str(item.get("dst") or ""),
                str(item.get("relation") or ""), str(item.get("file") or ""),
                int(item.get("line") or 0),
            ),
        ),
        "memory_links": sorted(
            (entry[1] for entry in links.values()),
            key=lambda item: (
                str(item.get("repo_id") or ""), str(item.get("symbol_id") or ""),
                str(item.get("memory_id") or ""), str(item.get("relation") or ""),
            ),
        ),
        "analysis": latest.get("analysis") or {},
    }


def _merge(args) -> None:
    payloads = []
    for path in (args.base, args.current, args.other):
        try:
            payloads.append(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    Path(args.current).write_text(
        json.dumps(_merge_payloads(payloads), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _install_merge_driver(args) -> None:
    root = Path(args.root).expanduser().resolve()
    subprocess.run([
        "git", "-C", str(root), "config", "merge.engraphis-graph.name",
        "Engraphis code graph union merge",
    ], check=True)
    subprocess.run([
        "git", "-C", str(root), "config", "merge.engraphis-graph.driver",
        f'"{sys.executable}" -m scripts.graph_cli merge %O %A %B',
    ], check=True)
    attributes = root / ".gitattributes"
    current = attributes.read_text(encoding="utf-8") if attributes.exists() else ""
    rule = f"{args.graph_path} merge=engraphis-graph"
    if rule not in current.splitlines():
        attributes.write_text(current.rstrip() + ("\n" if current.strip() else "") + rule + "\n",
                              encoding="utf-8")
    print(f"Installed union merge driver for {args.graph_path}")


def _common(parser) -> None:
    parser.add_argument("--workspace", "-w", required=True)
    parser.add_argument("--repo", "-r", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engraphis-graph")
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("index", help="Incrementally index a repository")
    _common(item)
    item.add_argument("--root", required=True)
    item.add_argument("--languages", nargs="*")
    item.set_defaults(func=_index)

    item = sub.add_parser("search", help="Search symbols and linked memories")
    _common(item)
    item.add_argument("query")
    item.add_argument("--limit", type=int, default=20)
    item.set_defaults(func=_search)

    item = sub.add_parser("query", help="Query the unified memory and code graph")
    _common(item)
    item.add_argument("query")
    item.add_argument(
        "--intent", default="locate_code",
        choices=("recall", "locate_code", "explain", "summarize_history"),
    )
    item.add_argument("--limit", type=int, default=20)
    item.set_defaults(func=_query)

    item = sub.add_parser("explain", help="Explain a topic from scoped memory and graph evidence")
    _common(item)
    item.add_argument("query")
    item.add_argument("--limit", type=int, default=20)
    item.set_defaults(func=_query, intent="explain")

    item = sub.add_parser("path", help="Find a code/memory graph path")
    _common(item)
    item.add_argument("source")
    item.add_argument("target")
    item.add_argument("--max-depth", type=int, default=8)
    item.set_defaults(func=_path)

    item = sub.add_parser("impact", help="Analyze explicit files or a git diff")
    _common(item)
    item.add_argument("files", nargs="*")
    item.add_argument("--root", default=".")
    item.add_argument("--git-range", nargs="?", const="HEAD", default=None)
    item.set_defaults(func=_impact)

    item = sub.add_parser("prs", help="Triage a branch or compare conflict zones")
    _common(item)
    item.add_argument("--root", default=".")
    item.add_argument("--base", default="main")
    item.add_argument("--head", default="HEAD")
    item.add_argument("--conflicts-with")
    item.set_defaults(func=_prs)

    item = sub.add_parser("export", help="Write graph.json, graph.html, and report")
    _common(item)
    item.add_argument("--output", "-o", default="engraphis-graph-out")
    item.set_defaults(func=_export)

    item = sub.add_parser("postgres", help="Ingest a live PostgreSQL catalog")
    _common(item)
    item.add_argument("--dsn-env", default="ENGRAPHIS_POSTGRES_DSN")
    item.add_argument("--schemas", nargs="*")
    item.set_defaults(func=_postgres)

    item = sub.add_parser("merge", help=argparse.SUPPRESS)
    item.add_argument("base")
    item.add_argument("current")
    item.add_argument("other")
    item.set_defaults(func=_merge)

    item = sub.add_parser("install-merge-driver")
    item.add_argument("--root", default=".")
    item.add_argument("--graph-path", default="engraphis-graph-out/graph.json")
    item.set_defaults(func=_install_merge_driver)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
        return 0
    except (ValidationError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
