"""Offline closed-history topic recall past the 500-candidate recency window.

Run with ``python -m eval.closed_history``. This measures lexical candidate recall,
not exhaustive semantic recall or a bound on SQLite's scoped scan/sort work.
"""
from __future__ import annotations

import json

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope


def evaluate() -> dict:
    engine = MemoryEngine.create(":memory:")
    try:
        workspace = engine.store.get_or_create_workspace("history-eval")
        foreign = engine.store.get_or_create_workspace("private-eval")

        def add(content: str, *, title: str = "", ingested_at: float = 1.0,
                workspace_id: str = workspace, approved: bool = True) -> str:
            return engine.store.add_memory(MemoryRecord(
                id="", content=content, title=title, workspace_id=workspace_id,
                scope=Scope.WORKSPACE, valid_from=-1.0, valid_to=0.0,
                valid_to_recorded_at=1.0, ingested_at=ingested_at,
                provenance={"trusted": approved,
                            "review_state": "approved" if approved else "pending"},
            ))

        content_id = add("quasar launch protocol")
        title_id = add("A preserved historical decision.", title="cerulean deploy region")
        forbidden = set()
        for index in range(501):
            stamp = 2.0 + index
            add(f"Gardening weather bulletin {index}", ingested_at=stamp)
            forbidden.add(add("quasar launch protocol", ingested_at=stamp, approved=False))
            forbidden.add(add("cerulean deploy region", ingested_at=stamp, workspace_id=foreign))

        cases = [
            ("quasar launch protocol", content_id),
            ("PROTOCOL launch quasar", content_id),
            ("cerulean deploy region", title_id),
        ]
        hits = 0
        leaks = 0
        for query, expected in cases:
            result = engine.timeline(
                query, workspace_id=workspace, closed_only=True, prompt_only=True,
                limit=1, known_at=1000.0, valid_at=1000.0,
            )
            hits += int([record.id for record in result] == [expected])
            leaks += sum(record.id in forbidden for record in result)
        return {"cases": len(cases), "newer_unrelated_closed": 501,
                "topic_hit_at_1": hits / len(cases), "excluded_record_leaks": leaks}
    finally:
        engine.store.close()


if __name__ == "__main__":
    report = evaluate()
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["topic_hit_at_1"] == 1.0
                     and report["excluded_record_leaks"] == 0 else 1)
