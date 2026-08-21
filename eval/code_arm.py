"""Code arm eval: does the fourth hybrid arm earn its fusion slot?

AGENTS.md §3.7 demands a number for every capability claim.  The recall engine
ships four retrieval arms (vector, lexical, graph, code) but ``balanced`` and
``fast`` hardcode ``code=False``, so until now no eval measured the code arm at
all.  This module builds that number.

The fixture is honest by construction:

- The tiny repo is indexed through the REAL production code-indexing path
  (``MemoryEngine.index_repo`` with the AST/tree-sitter backend), producing
  symbols plus ``calls`` edges.
- Memories are written through ``engine.remember``, so the production write-time
  ``_link_memory_to_code`` bridge — not a fixture shortcut — creates the
  symbol<->memory links.
- Each question is answerable ONLY through the code arm: the supporting memory
  shares no tokens with the query, so the lexical (FTS) and vector (hashing)
  arms cannot rank it, but it is linked to a symbol one ``calls`` hop away from
  the symbol the query names.  Distractor memories deliberately contain the
  query's surface words, so both text arms fill their top-k with plausible
  near-misses.

Pass criteria (strict lift, measured — not tautological):

1. code-arm recall@k == 1.0 (the bridge always reaches the supporting memory),
2. strictly above the vector and lexical arms on the same fixture,
3. and the full pipeline with ``retrieval_profile="code"`` must beat
   ``retrieval_profile="balanced"`` (the code=False default) at retrieving the
   same supporting memories.

If the code arm cannot show deterministic lift on this fixture, the eval exits 1
and any "four arms" claim must be scoped back to three.

    python -m eval.code_arm
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import SearchFilter
from eval import metrics
from eval.harness import load_dataset

DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "code_arm.jsonl"
DEFAULT_K = 5


def _seed_case(case: dict, root: Path) -> tuple[MemoryEngine, str, str, dict[str, str]]:
    """Index the fixture repo and write its memories through the real paths.

    Returns ``(engine, workspace_id, repo_id, tag_by_id)``.  The caller owns the
    engine lifecycle (``engine.store.close()``).
    """
    for f in case["files"]:
        path = root / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["code"] + "\n", encoding="utf-8")
    engine = MemoryEngine.create(":memory:")
    try:
        workspace_id = engine.store.get_or_create_workspace("eval")
        repo_id = engine.store.get_or_create_repo(workspace_id, case.get("id", "case"))
        info = engine.index_repo(repo_id, str(root), prefer="auto")
        if not info.get("symbols_indexed"):
            raise RuntimeError(
                f"case {case.get('id')!r}: code indexing produced zero symbols; "
                "the code-arm fixture cannot be measured"
            )
        tag_by_id: dict[str, str] = {}
        for m in case["memories"]:
            memory_id = engine.remember(m["text"], workspace_id=workspace_id, repo_id=repo_id)
            tag_by_id[memory_id] = m.get("tag")
    except Exception:
        engine.store.close()
        raise
    return engine, workspace_id, repo_id, tag_by_id


def _arm_recall(dataset: list[dict], *, k: int, arm: str) -> float:
    """Arm-level recall@k: can this SINGLE arm reach the supporting memory?

    Mirrors ``eval.ablation._arm_recall`` but for the code arm and its text-arm
    baselines.  ``arm``: "vector" (dense only), "lexical" (FTS only), or "code"
    (symbol/call bridge only, via ``RecallEngine._code_arm``).
    """
    if arm not in {"vector", "lexical", "code"}:
        raise ValueError(f"unknown arm: {arm!r}")
    per: list[float] = []
    for case in dataset:
        with tempfile.TemporaryDirectory() as td:
            engine, workspace_id, repo_id, tag_by_id = _seed_case(case, Path(td))
            try:
                flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
                for q in case["questions"]:
                    if arm == "vector":
                        ids = [
                            i for i, _ in engine.index.search(
                                engine.embedder.embed([q["q"]])[0], k, filter=flt)
                        ]
                    elif arm == "lexical":
                        ids = [i for i, _ in engine.store.fts_search(q["q"], k, filter=flt)]
                    else:
                        ids = list(engine.recall_engine._code_arm(q["q"], flt, k))
                    per.append(metrics.recall_at_k(
                        [tag_by_id.get(i) for i in ids], q.get("supporting", [])))
            finally:
                engine.store.close()
    return round(sum(per) / max(len(per), 1), 4)


def _profile_recall(dataset: list[dict], *, k: int, profile: str) -> float:
    """Full-pipeline recall@k with a named retrieval profile (arms fused).

    ``balanced`` is the shipping default with ``code=False``; ``code`` enables
    the fourth arm on top of the same fusion.  The delta between them is the
    pipeline-level contribution of the code arm.
    """
    per: list[float] = []
    for case in dataset:
        with tempfile.TemporaryDirectory() as td:
            engine, workspace_id, repo_id, tag_by_id = _seed_case(case, Path(td))
            try:
                flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
                for q in case["questions"]:
                    result = engine.recall_engine.recall(
                        q["q"], flt, k=k, reinforce=False, retrieval_profile=profile,
                    )
                    ids = [c["id"] for c in result.chunks]
                    per.append(metrics.recall_at_k(
                        [tag_by_id.get(i) for i in ids], q.get("supporting", [])))
            finally:
                engine.store.close()
    return round(sum(per) / max(len(per), 1), 4)


def evaluate(dataset: list[dict] | None = None, *, k: int = DEFAULT_K) -> dict:
    """Run the code-arm ablation and return metrics plus the pass decision."""
    ds = dataset if dataset is not None else load_dataset(str(DATASET_PATH))
    arms = {name: _arm_recall(ds, k=k, arm=name)
            for name in ("vector", "lexical", "code")}
    profiles = {name: _profile_recall(ds, k=k, profile=name)
                for name in ("balanced", "code")}
    passed = (
        arms["code"] == 1.0
        and arms["code"] > arms["vector"]
        and arms["code"] > arms["lexical"]
        and profiles["code"] > profiles["balanced"]
    )
    return {"k": k, "arms": arms, "profiles": profiles, "passed": passed}


def _print_report(result: dict) -> None:
    k = result["k"]
    print(f"code arm eval (recall@{k}, offline deterministic fixture)")
    print(f"  arm-isolated recall@{k}:")
    for name in ("vector", "lexical", "code"):
        print(f"    {name:<8} {result['arms'][name]:.4f}")
    print(f"  full-pipeline recall@{k}:")
    for name in ("balanced", "code"):
        print(f"    {name:<8} {result['profiles'][name]:.4f}")
    verdict = "PASS" if result["passed"] else "FAIL"
    print(f"  strict lift (code arm reaches what vector/lexical miss): {verdict}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.code_arm",
        description="Measure the code retrieval arm against the text arms.",
    )
    parser.add_argument("--dataset", default=str(DATASET_PATH),
                        help="JSONL fixture path (default: eval/datasets/code_arm.jsonl)")
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="recall@k depth")
    args = parser.parse_args(argv)
    result = evaluate(load_dataset(args.dataset), k=max(1, args.k))
    _print_report(result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
