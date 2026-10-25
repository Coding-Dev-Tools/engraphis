"""Offline exact-identifier retrieval gate for memories shared by desktop agents."""
from __future__ import annotations

import json

from engraphis.service import MemoryService


def evaluate() -> dict:
    cases = []
    for identifier in ("20261020-ENGRAPHIS-7QF", "desktop_memory_7QF", "desktop/memory/7QF", "desktop.memory.7QF"):
        for use_fts in (True, False):
            service = MemoryService.create(":memory:")
            try:
                service.store.has_fts5 = use_fts and service.store.has_fts5
                common = {"workspace": "desktop-test", "repo": "shared",
                          "_local_agent_operator": True, "_ingress": "mcp",
                          "resolve_conflicts": False}
                target = service.remember(
                    f"Desktop integration diagnostic marker {identifier}: copper lantern.",
                    mtype="working", importance=0.2, **common,
                )["id"]
                for text in (
                    "Engraphis stores semantic preferences for the release.",
                    "Engraphis keeps procedural notes about packaging.",
                    "Engraphis tracks deployment decisions for teams.",
                    "Engraphis records architecture choices in the desktop product.",
                    "Engraphis remembers project plans and troubleshooting history.",
                ):
                    service.remember(text, mtype="semantic", importance=0.9, **common)
                result = service.recall(identifier, workspace="desktop-test", repo="shared", k=3,
                                        token_budget=400)
                ids = [memory["id"] for memory in result["memories"]]
                cases.append({"identifier": identifier, "fts": service.store.has_fts5,
                              "recall_at_1": target in ids[:1], "recall_at_3": target in ids[:3],
                              "packed_recall_at_1": bool(result["packed_sources"])
                              and result["packed_sources"][0]["id"] == target})
            finally:
                service.close()
    return {"cases": cases, "recall_at_1": sum(c["recall_at_1"] for c in cases) / len(cases),
            "recall_at_3": sum(c["recall_at_3"] for c in cases) / len(cases),
            "packed_recall_at_1": sum(c["packed_recall_at_1"] for c in cases) / len(cases)}


def main() -> int:
    result = evaluate()
    print(json.dumps(result, indent=2))
    return 0 if result["recall_at_1"] == result["recall_at_3"] == result["packed_recall_at_1"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
