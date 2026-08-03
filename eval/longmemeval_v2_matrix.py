"""Materialize the four-ablation, five-budget official LongMemEval-V2 config matrix."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Optional

from eval.benchmark import CANONICAL_TOKEN_BUDGETS
from eval.run_longmemeval_v2 import PINNED_READER_MODEL, PINNED_READER_REVISION


BASE_CONFIGS = {
    "balanced": "longmemeval_v2_engraphis.json",
    "planner": "longmemeval_v2_engraphis_planner.json",
    "type_limits": "longmemeval_v2_engraphis_type_limits.json",
    "planner_type_limits": "longmemeval_v2_engraphis_planner_type_limits.json",
}


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def prepare(output_dir: str | Path) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    config_root = Path(__file__).resolve().parent / "configs"
    rows = []
    for ablation, filename in BASE_CONFIGS.items():
        base = json.loads((config_root / filename).read_text(encoding="utf-8"))
        params = base.get("memory_params") or {}
        if (
            params.get("reader_tokenizer_model") != PINNED_READER_MODEL
            or params.get("reader_tokenizer_revision") != PINNED_READER_REVISION
        ):
            raise ValueError(f"{filename} does not use the pinned reader")
        for budget in CANONICAL_TOKEN_BUDGETS:
            config = json.loads(json.dumps(base))
            config["memory_params"]["max_context_tokens"] = budget
            content = _canonical(config)
            target = output / f"{ablation}-{budget}.json"
            if target.exists() and target.read_text(encoding="utf-8") != content:
                raise ValueError(f"refusing to replace different matrix config: {target}")
            target.write_text(content, encoding="utf-8")
            rows.append({
                "ablation": ablation,
                "token_budget": budget,
                "config": target.name,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            })
    manifest = {
        "name": "engraphis-longmemeval-v2-planned-recall-matrix/v1",
        "reader_model": PINNED_READER_MODEL,
        "reader_revision": PINNED_READER_REVISION,
        "token_budgets": list(CANONICAL_TOKEN_BUDGETS),
        "ablations": list(BASE_CONFIGS),
        "runs": rows,
    }
    manifest_content = _canonical(manifest)
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and manifest_path.read_text(encoding="utf-8") != manifest_content:
        raise ValueError(f"refusing to replace different matrix manifest: {manifest_path}")
    manifest_path.write_text(manifest_content, encoding="utf-8")
    return manifest


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = prepare(args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        raise SystemExit(2) from exc
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
