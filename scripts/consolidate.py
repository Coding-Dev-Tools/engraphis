#!/usr/bin/env python3
"""Sleep-time consolidation as a schedulable local job.

Your machine, your schedule — no cloud service involved. Examples::

    # See what would happen (recommended first run)
    python -m scripts.consolidate --db engraphis.db --workspace acme --dry-run

    # Run for real; distill recurring episodes + archive decayed transients
    python -m scripts.consolidate --db engraphis.db --workspace acme

    # Nicer digests via the configured LLM (falls back to deterministic on error)
    python -m scripts.consolidate --db engraphis.db --workspace acme --llm

    # Schema-first LLM distillation with entity/relation graph hints
    python -m scripts.consolidate --db engraphis.db --workspace acme --structured

    # Team: also write a human-readable summary report (.md or .html by extension)
    python -m scripts.consolidate --db engraphis.db --workspace acme \
        --report reports/consolidation-$(date +%F).md

Schedule it (cron)::         0 3 * * *  cd /path/to/repo && python -m scripts.consolidate --db engraphis.db --workspace acme
Schedule it (Windows)::      schtasks /Create /SC DAILY /ST 03:00 /TN EngraphisConsolidate /TR "python -m scripts.consolidate --db C:\\path\\engraphis.db --workspace acme"

The sweep itself is free-tier, always. ``--report`` (the scheduled ops artifact) is a
Team feature — the gate lives HERE, in the script, via the same ``require_feature``
helper the Inspector uses; the core engine below never checks licenses.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from engraphis.core.engine import MemoryEngine


def _live_count(engine: MemoryEngine, workspace_id: str, repo_id=None) -> int:
    """Live (non-expired, currently valid) memories in scope — the before/after metric."""
    sql = ("SELECT COUNT(*) AS n FROM memories WHERE workspace_id=? "
           "AND expired_at IS NULL AND (valid_from IS NULL OR valid_from<=?) "
           "AND (valid_to IS NULL OR ?<valid_to)")
    now = time.time()
    args = [workspace_id, now, now]
    if repo_id:
        sql += " AND repo_id=?"
        args.append(repo_id)
    return int(engine.store.conn.execute(sql, args).fetchone()["n"])


def _report_sections(report: dict, *, workspace: str, repo, before: int, after: int) -> list:
    """Report content as (title, rows-of-(label, value)) pairs — one source for md/html."""
    from engraphis import __version__
    digests = report.get("digests_created", [])
    archived = report.get("archived", [])
    comp = report.get("compaction", {})
    profiles = (report.get("profiles") or {}).get("profiles_created", [])
    structured = report.get("structured") or {}
    merged = sum(len(d.get("consolidates") or d.get("would_consolidate") or [])
                 for d in digests)
    generated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return [
        ("Run", [
            ("workspace", workspace),
            ("repo", repo or "(all)"),
            ("mode", "dry run — nothing changed" if report.get("dry_run") else "applied"),
            ("generated", f"{generated} · engraphis v{__version__}"),
        ]),
        ("Before / after", [
            ("live memories before", before),
            ("live memories after", after),
            ("net change", after - before),
        ]),
        ("Merged (episodic patterns → semantic digests)", [
            ("clusters found", report.get("clusters_found", 0)),
            ("digests created", len(digests)),
            ("memories merged into digests", merged),
            ("skipped (already consolidated)",
             report.get("skipped_already_consolidated", 0)),
        ]),
        ("Decayed & pruned (bi-temporal close — recoverable, audited)", [
            ("transients archived", len(archived)),
            ("tokens freed", comp.get("archived_tokens_freed", 0)),
        ]),
        ("Structured LLM distillation", [
            ("enabled", bool(structured.get("enabled"))),
            ("attempted", structured.get("attempted", 0)),
            ("succeeded", structured.get("succeeded", 0)),
            ("fallbacks", structured.get("fallbacks", 0)),
            ("sources superseded", structured.get("sources_superseded", 0)),
        ]),
        ("Entity profiles", [
            ("profiles created", len(profiles)),
        ]),
        ("Compaction", [
            ("tokens saved by distillation",
             comp.get("distilled", {}).get("tokens_saved", 0)),
            ("total tokens saved", comp.get("total_tokens_saved", 0)),
        ]),
    ]


def _render_md(sections: list) -> str:
    out = ["# Engraphis consolidation report", ""]
    for title, rows in sections:
        out += [f"## {title}", ""]
        out += [f"- **{label}:** {value}" for label, value in rows]
        out.append("")
    return "\n".join(out)


def _render_html(sections: list) -> str:
    import html as _html
    css = ("body{font:14px/1.5 system-ui,sans-serif;color:#1f2328;max-width:720px;"
           "margin:0;padding:28px}h1{font-size:20px}h2{font-size:14px;margin:22px 0 6px;"
           "border-bottom:1px solid #d0d7de;padding-bottom:3px}"
           "table{border-collapse:collapse;font-size:13px}"
           "td{padding:4px 12px 4px 0;vertical-align:top}td:first-child{color:#59636e}")
    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<title>Engraphis consolidation report</title>",
             f"<style>{css}</style></head><body>",
             "<h1>Engraphis consolidation report</h1>"]
    for title, rows in sections:
        cells = "".join(
            f"<tr><td>{_html.escape(str(label))}</td>"
            f"<td>{_html.escape(str(value))}</td></tr>" for label, value in rows)
        parts.append(f"<h2>{_html.escape(title)}</h2><table>{cells}</table>")
    parts.append("</body></html>")
    return "".join(parts)


def _write_report(path: Path, report: dict, *, workspace: str, repo,
                  before: int, after: int) -> None:
    sections = _report_sections(report, workspace=workspace, repo=repo,
                                before=before, after=after)
    render = _render_html if path.suffix.lower() in (".html", ".htm") else _render_md
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(sections), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one Engraphis consolidation sweep.")
    ap.add_argument("--db", required=True, help="Path to the v2 database file.")
    ap.add_argument("--workspace", required=True, help="Workspace name to consolidate.")
    ap.add_argument("--repo", default=None, help="Restrict to one repo name.")
    ap.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
    ap.add_argument("--min-cluster", type=int, default=3,
                    help="Recurrences before an episodic pattern is digested (default 3).")
    ap.add_argument("--archive-below", type=float, default=0.05,
                    help="Retention floor for archiving transients (default 0.05).")
    ap.add_argument("--llm", action="store_true",
                    help="Summarize digests with the configured LLM (.env) instead of "
                         "the deterministic digest text.")
    ap.add_argument("--profiles", action="store_true",
                    help="Also roll each entity's memories into one durable profile "
                         "digest (needs graph entities; report lands under 'profiles').")
    ap.add_argument("--structured", action="store_true",
                    help="Use configured LLM for schema-validated consolidation facts "
                         "with entities/relations/confidence; falls back to deterministic.")
    ap.add_argument("--supersede-sources", action="store_true",
                    help="Only with --structured: bi-temporally close source episodes "
                         "after validated facts are written.")
    ap.add_argument("--min-mentions", type=int, default=3,
                    help="Memories mentioning an entity before it earns a profile "
                         "(default 3; only used with --profiles).")
    ap.add_argument("--report", default=None, metavar="PATH",
                    help="Team: also write a markdown (or .html) summary report — "
                         "merged/decayed/pruned counts, before/after — to PATH. "
                         "The sweep itself stays free.")
    args = ap.parse_args(argv)
    if args.supersede_sources and not args.structured:
        print("error: --supersede-sources requires --structured", file=sys.stderr)
        return 2


    if args.report:
        # Team gate for the report artifact only — checked up front so a scheduled
        # run fails loudly before touching the database, never halfway through.
        from engraphis.licensing import LicenseError, require_feature
        try:
            require_feature("team")
        except LicenseError as exc:
            print(f"error: --report is a Team feature. {exc}", file=sys.stderr)
            return 2

    engine = MemoryEngine.create(args.db)
    wid_row = engine.store.conn.execute(
        "SELECT id FROM workspaces WHERE name=?", (args.workspace,)).fetchone()
    if not wid_row:
        print(f"error: no workspace named '{args.workspace}' in {args.db}", file=sys.stderr)
        return 2
    rid = None
    if args.repo:
        rid_row = engine.store.conn.execute(
            "SELECT id FROM repos WHERE workspace_id=? AND name=?",
            (wid_row["id"], args.repo)).fetchone()
        if not rid_row:
            print(f"error: no repo named '{args.repo}' in workspace "
                  f"'{args.workspace}'", file=sys.stderr)
            return 2
        rid = rid_row["id"]

    llm = None
    if args.llm or args.structured:
        # LLM-powered consolidation (inference / structured merging) is a paid
        # automation feature — gate here because we call engine.consolidate()
        # directly, which has no license awareness.
        from engraphis.licensing import require_feature, LicenseError
        try:
            require_feature("automation")
        except LicenseError as exc:
            print(f"error: LLM-powered consolidation ({exc})", file=sys.stderr)
            print("tip: run without --llm or --structured for the free, deterministic pass",
                  file=sys.stderr)
            return 2
        try:
            from engraphis.llm.client import LLMClient
            llm = LLMClient()
        except Exception as exc:  # noqa: BLE001
            print(f"warning: LLM unavailable ({exc}); using deterministic digests",
                  file=sys.stderr)

    before = _live_count(engine, wid_row["id"], rid)
    try:
        report = engine.consolidate(
            workspace_id=wid_row["id"], repo_id=rid, dry_run=args.dry_run,
            min_cluster=args.min_cluster, archive_below=args.archive_below, llm=llm,
            profiles=args.profiles, min_mentions=args.min_mentions,
            structured=args.structured, supersede_sources=args.supersede_sources,
        )
    finally:
        if llm is not None and hasattr(llm, "close"):
            try:
                llm.close()
            except Exception:
                pass
    print(json.dumps(report, indent=2))
    if args.report:
        after = _live_count(engine, wid_row["id"], rid)
        path = Path(args.report).expanduser()
        _write_report(path, report, workspace=args.workspace, repo=args.repo,
                      before=before, after=after)
        print(f"report written to {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
