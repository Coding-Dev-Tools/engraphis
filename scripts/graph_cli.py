"""Repository-graph CLI over the same v2 MemoryService used by MCP and the dashboard."""
from __future__ import annotations

import argparse
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional

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
    # Console JSON must survive the default Windows charmap codec. Non-ASCII graph
    # labels remain lossless JSON escapes instead of turning a successful command into
    # exit 2 when stdout cannot encode characters such as U+2192.
    print(json.dumps(value, indent=2, ensure_ascii=True, default=str))


# Revisions reach _git_files from agent-controlled input (impact --git-range,
# prs --base/--head/--conflicts-with); a value that makes `git diff` hang — a huge
# diff, a repo-local .gitconfig external diff driver or pager, a submodule credential
# prompt — must not block the process indefinitely.
_GIT_TIMEOUT_S = 30


def _git_files(root: str, revision: str) -> list[str]:
    cmd = [
        "git", "-C", str(Path(root).resolve()),
        "-c", "core.pager=cat", "-c", "pager.diff=cat",
        "diff", "--no-ext-diff", "--name-only", "-z",
    ]
    if revision:
        # The revision sits BEFORE the ``--`` separator, so git would parse a
        # leading-dash value as an option (e.g. ``--output=<file>`` writes an
        # arbitrary file). No legitimate revision or range starts with ``-``.
        if revision.startswith("-"):
            raise ValidationError(f"invalid git revision {revision!r}")
        cmd.append(revision)
    cmd.append("--")
    try:
        result = subprocess.run(cmd, capture_output=True, check=False,
                                timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ValidationError(f"git diff timed out after {_GIT_TIMEOUT_S}s")
    if result.returncode:
        raise ValidationError(os.fsdecode(result.stderr).strip() or "git diff failed")
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


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
    out = Path(args.output).expanduser()
    if out.is_symlink():
        # A pre-planted symlink at the output directory would redirect the whole
        # export elsewhere (e.g. a committed `engraphis-graph-out` link in a
        # hostile checkout run with the default -o). Refuse rather than follow.
        raise ValidationError(f"output path {out} is a symlink; refusing to export")
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # _atomic_write_text (temp + os.replace) never follows a pre-planted symlink
    # at the destination, unlike Path.write_text — same discipline as _merge.
    _atomic_write_text(
        out / "graph.json",
        json.dumps(result["graph"], indent=2, ensure_ascii=False, default=str),
    )
    _atomic_write_text(out / "GRAPH_REPORT.md", result["report_markdown"])
    _atomic_write_text(out / "graph.html", result["graph_html"])
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
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=str, allow_nan=False,
    )


def _timestamp(value) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _candidate_key(value: dict, parent_timestamp: float = 0.0) -> tuple[float, str]:
    updated_at = _timestamp(value.get("updated_at") or parent_timestamp)
    return updated_at, _stable_json(value)


MAX_MERGE_EXPORT_BYTES = 64 * 1024 * 1024
MAX_MERGE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_MERGE_FILES = 20_000
MAX_MERGE_NODES = 250_000
MAX_MERGE_EDGES = 500_000
MAX_MERGE_LINKS = 500_000
MAX_MERGE_JSON_DEPTH = 200


def _dict_items(value, limit: int) -> Iterable[dict]:
    if not isinstance(value, list):
        return
    for item in value[:limit]:
        if isinstance(item, dict):
            yield item


def _line_number(value) -> int:
    try:
        line = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(2_147_483_647, line))


def _merge_payloads(payloads: list[dict]) -> dict:
    payloads = [
        payload for payload in payloads
        if isinstance(payload, dict)
        and payload.get("format") in (None, "engraphis-code-graph/1")
    ]
    if not payloads:
        return {"format": "engraphis-code-graph/1", "files": [], "nodes": [],
                "edges": [], "memory_links": [], "analysis": {}}
    files = {}
    nodes = {}
    old_to_new = {}
    for payload in payloads:
        generated_at = _timestamp(payload.get("generated_at"))
        for item in _dict_items(payload.get("files"), MAX_MERGE_FILES):
            key = str(item.get("file") or "")
            if not key:
                continue
            chosen = files.get(key)
            if chosen is None and len(files) >= MAX_MERGE_FILES:
                continue
            if chosen is None or _candidate_key(item, generated_at) > chosen[0]:
                files[key] = (_candidate_key(item, generated_at), item)
        for node in _dict_items(payload.get("nodes"), MAX_MERGE_NODES):
            key = _natural_node(node)
            if not any(key):
                continue
            chosen = nodes.get(key)
            if chosen is None and len(nodes) >= MAX_MERGE_NODES:
                continue
            if chosen is None or _candidate_key(node, generated_at) > chosen[0]:
                nodes[key] = (_candidate_key(node, generated_at), node)
    for payload in payloads:
        for node in _dict_items(payload.get("nodes"), MAX_MERGE_NODES):
            chosen_entry = nodes.get(_natural_node(node))
            chosen = chosen_entry[1] if chosen_entry else None
            if node.get("id") and chosen and chosen.get("id"):
                if len(old_to_new) >= MAX_MERGE_NODES * 2:
                    continue
                old_to_new[str(node["id"])] = str(chosen["id"])
    edges = {}
    links = {}
    for payload in payloads:
        generated_at = _timestamp(payload.get("generated_at"))
        for edge in _dict_items(payload.get("edges"), MAX_MERGE_EDGES):
            item = dict(edge)
            src = str(item.get("src") or "")
            dst = str(item.get("dst") or "")
            if not src or not dst:
                continue
            item["src"] = old_to_new.get(src, src)
            item["dst"] = old_to_new.get(dst, dst)
            item["line"] = _line_number(item.get("line"))
            item["relation"] = str(item.get("relation") or "")
            item["file"] = str(item.get("file") or "")
            item["layer"] = str(item.get("layer") or "")
            key = (
                item.get("src"), item.get("dst"), item.get("relation"),
                item.get("file"), item.get("line"), item.get("layer"),
            )
            chosen = edges.get(key)
            if chosen is None and len(edges) >= MAX_MERGE_EDGES:
                continue
            if chosen is None or _candidate_key(item, generated_at) > chosen[0]:
                edges[key] = (_candidate_key(item, generated_at), item)
        for link in _dict_items(payload.get("memory_links"), MAX_MERGE_LINKS):
            item = dict(link)
            symbol_id = str(item.get("symbol_id") or "")
            memory_id = str(item.get("memory_id") or "")
            if not symbol_id or not memory_id:
                continue
            item["symbol_id"] = old_to_new.get(symbol_id, symbol_id)
            item["memory_id"] = memory_id
            item["repo_id"] = str(item.get("repo_id") or "")
            item["relation"] = str(item.get("relation") or "")
            key = (
                item.get("repo_id"), item.get("symbol_id"),
                item.get("memory_id"), item.get("relation"),
            )
            chosen = links.get(key)
            if chosen is None and len(links) >= MAX_MERGE_LINKS:
                continue
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
        "generated_at": _timestamp(latest.get("generated_at")),
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
                _line_number(item.get("line")), str(item.get("layer") or ""),
            ),
        ),
        "memory_links": sorted(
            (entry[1] for entry in links.values()),
            key=lambda item: (
                str(item.get("repo_id") or ""), str(item.get("symbol_id") or ""),
                str(item.get("memory_id") or ""), str(item.get("relation") or ""),
            ),
        ),
        "analysis": latest.get("analysis")
        if isinstance(latest.get("analysis"), dict) else {},
    }


def _scan_json_depth(text: str) -> int:
    depth = maximum = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            maximum = max(maximum, depth)
        elif char in "]}":
            depth = max(0, depth - 1)
    return maximum


def _reject_json_constant(value: str):
    raise ValueError("non-finite JSON constant: %s" % value)


def _load_merge_payload(path: str) -> Optional[dict]:
    source = Path(path)
    try:
        if source.stat().st_size > MAX_MERGE_EXPORT_BYTES:
            return None
        text = source.read_text(encoding="utf-8")
        if _scan_json_depth(text) > MAX_MERGE_JSON_DEPTH:
            return None
        payload = json.loads(text, parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload if payload.get("format") in (None, "engraphis-code-graph/1") else None


def _atomic_write_text(path: Path, text: str) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".engraphis.tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_regular_text(path: Path, *, max_bytes: int = 1024 * 1024) -> str:
    if not path.exists():
        return ""
    fd = -1
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise ValidationError(f"{path.name} must be a regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size > max_bytes
        ):
            raise ValidationError(f"{path.name} is unsafe or too large")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            text = fh.read(max_bytes + 1)
        if len(text.encode("utf-8")) > max_bytes:
            raise ValidationError(f"{path.name} is unsafe or too large")
        return text
    finally:
        if fd >= 0:
            os.close(fd)


def _merge(args) -> None:
    paths = [Path(args.base), Path(args.current), Path(args.other)]
    try:
        total_bytes = sum(path.stat().st_size for path in paths)
    except OSError as exc:
        raise ValidationError("merge inputs must all be readable files") from exc
    if total_bytes > MAX_MERGE_TOTAL_BYTES:
        raise ValidationError("combined graph merge inputs exceed the safety limit")
    base = _load_merge_payload(args.base)
    current_payload = _load_merge_payload(args.current)
    other = _load_merge_payload(args.other)
    if current_payload is None or other is None:
        raise ValidationError(
            "current and incoming graph exports must be valid bounded graph JSON"
        )
    payloads = [payload for payload in (base, current_payload, other) if payload is not None]
    current = Path(args.current)
    serialized = json.dumps(
        _merge_payloads(payloads), indent=2, ensure_ascii=False,
        default=str, allow_nan=False,
    )
    _atomic_write_text(current, serialized)


def _install_merge_driver(args) -> None:
    root = Path(args.root).expanduser().resolve()
    attributes = root / ".gitattributes"
    current = _read_regular_text(attributes)
    graph_path = str(args.graph_path or "").strip()
    if (
        not graph_path
        or graph_path.startswith("!")
        or any(char in graph_path for char in "\x00\r\n")
    ):
        raise ValidationError("graph path must be a non-empty single-line path")
    pattern = (
        json.dumps(graph_path)
        if graph_path.startswith("#") or any(char.isspace() for char in graph_path)
        else graph_path
    )
    rule = f"{pattern} merge=engraphis-graph"
    try:
        subprocess.run([
            "git", "-C", str(root), "config", "merge.engraphis-graph.name",
            "Engraphis code graph union merge",
        ], check=True, timeout=_GIT_TIMEOUT_S)
        subprocess.run([
            "git", "-C", str(root), "config", "merge.engraphis-graph.driver",
            f'"{sys.executable}" -m scripts.graph_cli merge "%O" "%A" "%B"',
        ], check=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ValidationError(f"git config timed out after {_GIT_TIMEOUT_S}s")
    if rule not in current.splitlines():
        _atomic_write_text(
            attributes,
            current.rstrip() + ("\n" if current.strip() else "") + rule + "\n",
        )
    print(f"Installed union merge driver for {args.graph_path}")


def _common(parser) -> None:
    parser.add_argument("--workspace", "-w", required=True)
    parser.add_argument("--repo", "-r", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engraphis-graph")
    visible = ("index", "search", "query", "explain", "path", "impact", "prs",
               "export", "postgres", "install-merge-driver")
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{%s}" % ",".join(visible)
    )

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

    # argparse.SUPPRESS still renders ``merge ==SUPPRESS==`` for subparsers. Keep the
    # command callable by git while removing only its help pseudo-action.
    sub._choices_actions = [a for a in sub._choices_actions if a.dest != "merge"]

    item = sub.add_parser("install-merge-driver", help="Install the graph union merge driver")
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
