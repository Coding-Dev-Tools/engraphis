"""Retroactively apply the memory-poisoning trust boundary to an existing v2 store.

The normal write path labels new ingress and quarantines suspicious untrusted text.
Older databases may contain unlabelled rows or records imported before that policy
existed.  This tool is deliberately dry-run by default: inspect the aggregate report,
then rerun with ``--apply`` during a maintenance window.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from engraphis.config import settings
from engraphis.core.poisoning import (
    apply_quarantine_metadata,
    assess_untrusted_payload,
    provenance_is_trusted,
    source_is_external,
)
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store, now_ts


def _iter_records(store: Store, workspace_id: Optional[str]):
    """Yield bounded keyset pages, including already-invalid historical rows."""
    flt = SearchFilter(workspace_id=workspace_id) if workspace_id is not None else None
    after_id = ""
    while True:
        page = store.list_memories_page(
            flt, after_id=after_id, limit=500, include_invalid=True,
        )
        if not page:
            return
        for record in page:
            yield record
        after_id = page[-1].id


def rescan(db_path: str, *, apply: bool = False,
           only_workspace: Optional[str] = None,
           mark_unverified: bool = True) -> dict:
    """Report or apply durable untrusted/quarantine labels to existing records.

    ``mark_unverified`` fails closed for rows with no explicit trust provenance.
    Applying never deletes content: a detected record becomes a zero-validity,
    audited history entry and its stored vector is removed.
    """
    if db_path != ":memory:" and not Path(db_path).is_file():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    store = Store(db_path)
    try:
        workspace_id = None
        if only_workspace:
            row = store.conn.execute(
                "SELECT id FROM workspaces WHERE name=?", (only_workspace,)
            ).fetchone()
            if row is None:
                raise ValueError(f"no workspace named '{only_workspace}'")
            workspace_id = row["id"]
        summary = {
            "db": db_path,
            "apply": bool(apply),
            "scanned": 0,
            "already_quarantined": 0,
            "downgraded_untrusted": 0,
            "unverified": 0,
            "quarantine_candidates": 0,
            "quarantined": 0,
            "unchanged": 0,
        }
        for record in _iter_records(store, workspace_id):
            summary["scanned"] += 1
            metadata = dict(record.metadata or {})
            provenance = dict(record.provenance or metadata.get("provenance") or {})
            source = str(provenance.get("source") or "").strip()
            explicitly_untrusted = provenance.get("trusted") is False
            unverified = not provenance_is_trusted(provenance)
            external = source_is_external(source)
            should_downgrade = explicitly_untrusted or external or (
                mark_unverified and unverified
            )
            if unverified:
                summary["unverified"] += 1
            if not should_downgrade:
                summary["unchanged"] += 1
                continue

            provenance.update({
                "source": source or "legacy_unverified",
                "trusted": False,
                "trust_origin": "rescan_unverified",
            })
            metadata["provenance"] = dict(provenance)
            decision = assess_untrusted_payload(
                record.content, title=record.title, metadata=metadata
            )
            already_quarantined = bool(
                provenance.get("quarantined")
                or isinstance(metadata.get("quarantine"), dict)
                and metadata["quarantine"].get("state") == "quarantined"
            )
            if already_quarantined:
                summary["already_quarantined"] += 1
            elif decision.quarantined:
                summary["quarantine_candidates"] += 1
            else:
                summary["downgraded_untrusted"] += 1

            if not apply:
                continue
            if decision.quarantined and not already_quarantined:
                metadata = apply_quarantine_metadata(metadata, decision)
                # Close a live record now, but retain a prior governed closure instead
                # of rewriting historical validity to the scan time.
                quarantined_at = now_ts()
                effective_valid_to = record.valid_to or quarantined_at
                store.conn.execute(
                    "UPDATE memories SET metadata=?, provenance=?, "
                    "valid_to=COALESCE(valid_to, ?), "
                    "valid_to_recorded_at=CASE WHEN valid_to IS NULL THEN ? "
                    "ELSE valid_to_recorded_at END WHERE id=?",
                    (
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(metadata["provenance"], ensure_ascii=False,
                                   separators=(",", ":")),
                        quarantined_at, quarantined_at, record.id,
                    ),
                )
                store.conn.execute("DELETE FROM mem_vectors WHERE id=?", (record.id,))
                store.retire_memory_graph_state(
                    record.id, at=effective_valid_to, commit=False
                )
                store.audit(
                    "poisoning_rescan", "quarantine", record.id,
                    "policy=%s; reasons=%s" % (
                        decision.policy, ",".join(decision.reasons)
                    ),
                    commit=False,
                )
                summary["quarantined"] += 1
            else:
                store.conn.execute(
                    "UPDATE memories SET metadata=?, provenance=? WHERE id=?",
                    (
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(metadata["provenance"], ensure_ascii=False,
                                   separators=(",", ":")),
                        record.id,
                    ),
                )
                store.retire_memory_graph_state(record.id, commit=False)
                store.audit(
                    "poisoning_rescan", "trust_downgrade", record.id,
                    "source=%s" % (source or "legacy_unverified"), commit=False,
                )
        if apply:
            store.conn.commit()
        return summary
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retroactively label/quarantine untrusted memory payloads."
    )
    parser.add_argument("--db", default=settings.db_path, help="SQLite DB path")
    parser.add_argument("--only", metavar="WORKSPACE", default=None,
                        help="restrict to one workspace name")
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default is dry-run)")
    parser.add_argument("--keep-unlabelled", action="store_true",
                        help="do not downgrade legacy rows with no explicit trust label")
    args = parser.parse_args()
    print(json.dumps(rescan(
        args.db, apply=args.apply, only_workspace=args.only,
        mark_unverified=not args.keep_unlabelled,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
