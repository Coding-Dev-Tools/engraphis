"""CLI entrypoint for running Spec Crawl on agent instructions/specs or workspace memory nodes.

Usage:
    # Single-text prompt/spec crawl:
    python -m scripts.spec_crawl AGENTS.md
    python -m scripts.spec_crawl AGENTS.md --json
    python -m scripts.spec_crawl AGENTS.md --min-score 50
    cat prompt.md | python -m scripts.spec_crawl -

    # Multi-node memory cluster audit:
    python -m scripts.spec_crawl --workspace acme
    python -m scripts.spec_crawl --workspace acme --repo engraphis --json
    python -m scripts.spec_crawl --workspace acme --min-score 70
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engraphis.core.spec_crawl import (
    DEFAULT_TEMPLATE,
    MAX_SPEC_CHARS,
    crawl_spec,
)


def _audit_workspace_memories(
    workspace: str,
    repo: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    include_trace: bool = True,
) -> dict:
    from engraphis.config import settings
    from engraphis.service import MemoryService

    actual_db = db_path or getattr(settings, "db_path", "engraphis.db") or "engraphis.db"
    svc = MemoryService.create(
        actual_db,
        embed_model=settings.embed_model or None,
        embed_revision=getattr(settings, "embed_revision", "") or None,
        require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
        embed_dim=settings.embed_dim or 384,
        vector_backend=settings.vector_backend,
    )
    return svc.spec_crawl_memories(
        workspace=workspace,
        repo=repo,
        session_id=session_id,
        include_trace=include_trace,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Engraphis Spec Crawl on a spec file, prompt, or workspace memory cluster.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Path to markdown/text spec file, or '-' to read from standard input. Omit if using --workspace.",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        default=None,
        help="Audit active memory nodes from this workspace instead of a spec file.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Optional repo filter when auditing memory nodes.",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Optional session filter when auditing memory nodes.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (defaults to config or engraphis.db).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit raw JSON report instead of formatted text.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Fail with non-zero exit code if score / cluster health is below this threshold.",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="Omit detailed step trace from output to save space.",
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    if args.workspace:
        try:
            report = _audit_workspace_memories(
                workspace=args.workspace,
                repo=args.repo,
                session_id=args.session,
                db_path=args.db,
                include_trace=not args.no_trace,
            )
        except Exception as exc:
            sys.stderr.write(f"Error auditing workspace memories: {exc}\n")
            return 2

        if args.json_output:
            print(json.dumps(report, indent=2))
        else:
            health = report.get("cluster_health", 0)
            n_nodes = report.get("node_count", 0)
            print(f"=== Spec Crawl Memory Cluster Audit: workspace='{args.workspace}' ===")
            print(f"Cluster Health: {health}/100 ({n_nodes} active memory nodes)")

            print("\n--- 7-Axis Memory Radar ---")
            for axis, val in report.get("radar", {}).items():
                bar = "#" * int(val * 10) + "-" * (10 - int(val * 10))
                print(f"  {axis.upper():<12} [{bar}] {val * 100:.0f}%")

            policy_gaps = report.get("policy_gaps", [])
            if policy_gaps:
                print(f"\n--- Policy Gaps ({len(policy_gaps)}) ---")
                for g in policy_gaps:
                    rec = g.get("recommendation", "").replace("\u2014", "-").replace("\u2013", "-")
                    print(f"  [GAP] {g.get('axis', '').upper()}: {rec}")

            conflicts = report.get("conflicts", [])
            if conflicts:
                print(f"\n--- Cross-Node Conflicts ({len(conflicts)}) ---")
                for c in conflicts:
                    clean_detail = (c.get("reason") or c.get("detail", "")).replace("\u2014", "-").replace("\u2013", "-")
                    remedy = c.get("remedy", {})
                    remedy_detail = (remedy.get("recommendation") or remedy.get("detail", "")).replace("\u2014", "-").replace("\u2013", "-")
                    print(f"  [CONFLICT] {c.get('node_a')} <-> {c.get('node_b')} (severity: {c.get('severity')})")
                    print(f"    Detail: {clean_detail}")
                    print(f"    Remedy: {remedy.get('action', '').upper()} -> {remedy_detail}")

            redundancies = report.get("redundancies", [])
            if redundancies:
                print(f"\n--- Redundancies ({len(redundancies)}) ---")
                for r in redundancies:
                    print(f"  [DUPLICATE] {r.get('node_a')} <-> {r.get('node_b')} (similarity: {r.get('similarity', 0):.2f})")

            orphans = report.get("orphans", [])
            if orphans:
                print(f"\n--- Graph Orphans ({len(orphans)}) ---")
                for o in orphans:
                    sugg = ""
                    if o.get("suggested_link_to"):
                        sugg = f" (suggest link to {o.get('suggested_link_to')} - sim {o.get('similarity', 0):.2f})"
                    orphan_id = o.get("node_id") or o.get("memory_id")
                    print(f"  [ORPHAN] {orphan_id}{sugg}")

        if args.min_score is not None and report.get("cluster_health", 0) < args.min_score:
            sys.stderr.write(
                f"\nFAILURE: Cluster health {report.get('cluster_health', 0)} is below required minimum {args.min_score}\n"
            )
            return 1
        return 0

    if not args.file:
        parser.error("Either a spec file path or --workspace must be provided.")

    if args.file == "-":
        content = sys.stdin.read(MAX_SPEC_CHARS + 1)
        source_label = "stdin"
    else:
        path = Path(args.file)
        if not path.is_file():
            sys.stderr.write(f"Error: file not found: {args.file}\n")
            return 2
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                content = source.read(MAX_SPEC_CHARS + 1)
        except OSError as exc:
            sys.stderr.write(f"Error reading spec file: {exc}\n")
            return 2
        source_label = str(path)

    try:
        report = crawl_spec(
            content,
            template=DEFAULT_TEMPLATE,
            include_trace=not args.no_trace,
            source_label=source_label,
        )
    except Exception as exc:
        sys.stderr.write(f"Error running spec crawl: {exc}\n")
        return 2

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        score = report["score"]
        bd = report["score_breakdown"]
        counts = report["counts"]
        print(f"=== Spec Crawl: {source_label} ===")
        print(f"Spec Score: {score}/100")
        print(
            f"Breakdown:  coverage={bd.get('coverage', 0):.1f} "
            f"ownership={bd.get('ownership', 0):.1f} "
            f"approvals={bd.get('approvals', 0):.1f} "
            f"penalty={bd.get('penalty', 0):.1f}"
        )
        print(
            f"Counts:     words={counts['read']} linked={counts['linked']} "
            f"flags={counts['flagged']} links={counts['links']} "
            f"cross_links={counts['cross_links']}"
        )
        print("\n--- Coverage Radar ---")
        for axis, val in report["coverage"].items():
            bar = "#" * int(val * 10) + "-" * (10 - int(val * 10))
            label = report["axis_labels"].get(axis, axis.upper())
            print(f"  {label:<6} [{bar}] {val * 100:.0f}%")

        print("\n--- Sections ---")
        for s in report["sections"]:
            flag_marker = f" ({s['flagged']} flags)" if s["flagged"] else ""
            clean_title = s["title"].replace("\u2014", "-").replace("\u2013", "-")
            print(
                f"  [{s['index']:02d}] {clean_title:<35} "
                f"axis={s['axis']:<8} words={s['words']:<4}{flag_marker}"
            )

        if report["flags"]:
            print(f"\n--- Flags ({len(report['flags'])}) - ask, don't guess ---")
            for f in report["flags"]:
                clean_q = f["question"].replace("\u2014", "-").replace("\u2013", "-")
                print(f"  [{f['id']}] [{f['kind'].upper()}] {clean_q}")

    if args.min_score is not None and report["score"] < args.min_score:
        sys.stderr.write(
            f"\nFAILURE: Spec score {report['score']} is below required minimum {args.min_score}\n"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
