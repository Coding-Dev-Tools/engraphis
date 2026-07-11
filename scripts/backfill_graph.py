"""One-time backfill: populate the knowledge graph (``entities``/``edges``) from
memories already on disk, per workspace, using the dependency-free
``RegexGraphExtractor``.

Why this exists
---------------
Normal ingest only writes graph rows when ``ENGRAPHIS_GRAPH_EXTRACTOR`` is not
``"none"`` (see ``core/engine.py`` -- ``if self.graph_extractor is not None``).
When extraction is off, memories are stored but no entities are ever created, so
``MemoryService.graph`` returns an empty node set and the dashboard's Graph tab
shows *"No entities in this workspace yet."* -- even for a workspace with many
memories. This script closes that gap for memories that were written while
extraction was off. Set ``ENGRAPHIS_GRAPH_EXTRACTOR=regex`` in ``.env`` so future
ingests stay populated.

Safe to re-run: ``feed()`` de-duplicates entities by
``(workspace_id, repo_id, name, etype)`` and skips edges that already exist, so a
second pass writes nothing new.

Usage
-----
    python -m scripts.backfill_graph                 # live DB (ENGRAPHIS_DB_PATH)
    python -m scripts.backfill_graph --dry-run       # preview only, write nothing
    python -m scripts.backfill_graph --db PATH       # target a specific DB file
    python -m scripts.backfill_graph --only WS_NAME  # one workspace only

Run with the dashboard stopped to avoid writer contention on the live DB.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Optional

from engraphis.backends.graph_extractor import feed, get_graph_extractor
from engraphis.config import settings
from engraphis.core.store import Store


def backfill(db_path: str, *, dry_run: bool = False,
             only_workspace: Optional[str] = None) -> dict:
    """Extract entities/relations from every live memory and write them to the
    graph. Returns a per-workspace summary. ``dry_run`` runs the extractor but
    persists nothing (store writes auto-commit, so there is no transaction to roll
    back -- we simply skip the write)."""
    store = Store(db_path)
    conn = store.conn
    extractor = get_graph_extractor("regex")

    ws_names = {r["id"]: r["name"]
                for r in conn.execute("SELECT id, name FROM workspaces").fetchall()}

    sql = ("SELECT id, workspace_id, repo_id, title, content "
           "FROM memories WHERE expired_at IS NULL")
    params: list = []
    if only_workspace:
        row = conn.execute("SELECT id FROM workspaces WHERE name=?",
                           (only_workspace,)).fetchone()
        if row is None:
            store.close()
            raise SystemExit(f"no workspace named '{only_workspace}'")
        sql += " AND workspace_id=?"
        params.append(row["id"])

    rows = conn.execute(sql, params).fetchall()
    mem = defaultdict(int)
    ent = defaultdict(int)
    rel = defaultdict(int)

    for r in rows:
        wid = r["workspace_id"]
        mem[wid] += 1
        content, title = r["content"] or "", r["title"] or ""
        if dry_run:
            ex = extractor.extract(content, title=title)
            ent[wid] += len({e[0].lower() for e in ex.entities})
            rel[wid] += len(ex.relations)
        else:
            res = feed(store, content, workspace_id=wid, repo_id=r["repo_id"],
                       title=title, extractor=extractor,
                       provenance={"source": "backfill_graph"})
            ent[wid] += res["entities"]
            rel[wid] += res["relations"]

    totals = {r["workspace_id"]: r["n"] for r in conn.execute(
        "SELECT workspace_id, COUNT(*) n FROM entities GROUP BY workspace_id").fetchall()}
    store.close()

    workspaces = [{
        "workspace": ws_names.get(wid, wid),
        "memories_scanned": mem[wid],
        "entity_mentions": ent[wid],
        "relation_mentions": rel[wid],
        "entities_in_table_now": totals.get(wid, 0),
    } for wid in sorted(mem, key=lambda w: -mem[w])]
    return {"db": db_path, "dry_run": dry_run, "workspaces": workspaces}


def _print(summary: dict) -> None:
    mode = "DRY RUN (nothing written)" if summary["dry_run"] else "WROTE graph rows"
    print(f"\n{mode}  ->  {summary['db']}")
    print(f"{'workspace':<22}{'memories':>10}{'ent.ment':>10}{'rel.ment':>10}{'entities_now':>14}")
    print("-" * 66)
    for w in summary["workspaces"]:
        print(f"{w['workspace']:<22}{w['memories_scanned']:>10}{w['entity_mentions']:>10}"
              f"{w['relation_mentions']:>10}{w['entities_in_table_now']:>14}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill the knowledge graph from existing memories.")
    ap.add_argument("--db", default=settings.db_path,
                    help="SQLite DB path (default: ENGRAPHIS_DB_PATH / config)")
    ap.add_argument("--dry-run", action="store_true", help="preview counts, write nothing")
    ap.add_argument("--only", default=None, metavar="WORKSPACE",
                    help="restrict to a single workspace by name")
    args = ap.parse_args()
    _print(backfill(args.db, dry_run=args.dry_run, only_workspace=args.only))


if __name__ == "__main__":
    main()
