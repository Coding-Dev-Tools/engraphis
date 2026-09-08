"""Validate frozen independent-corpus declarations without inventing independent evidence."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Optional

from eval.benchmark import canonical_json, sha256_file


SCHEMA = "engraphis-coding-acceptance/v1"
CATEGORIES = (
    "corrections", "temporal_history", "scope_boundaries", "paraphrases",
    "code_relationships", "unsupported_questions", "poisoning", "condition_values",
    "long_documents", "multilingual",
)
ARMS = ("no_memory", "full_history", "lexical", "dense", "hybrid")
BUDGETS = (512, 1500, 4096)
SPLITS = {"development": 8, "validation": 8, "held_out": 24}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
ORIGINS = {"independent_human", "implementation_team", "synthetic_fixture"}


def schema() -> dict:
    """Portable JSON Schema for shape; validate_corpus enforces cross-row invariants."""
    identity = {"type": "string", "pattern": "^" + _ID.pattern.replace("\\Z", "$")}
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    scenario = {
        "type": "object", "additionalProperties": False,
        "required": ["id", "family_id", "category", "split", "origin", "author_ids",
                     "source_sha256", "oracle_sha256", "required_evidence_ids"],
        "properties": {
            "id": identity, "family_id": identity,
            "category": {"enum": list(CATEGORIES)}, "split": {"enum": list(SPLITS)},
            "origin": {"enum": sorted(ORIGINS)},
            "author_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                           "items": identity},
            "source_sha256": digest, "oracle_sha256": digest,
            "required_evidence_ids": {"type": "array", "uniqueItems": True, "items": identity},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": SCHEMA, "type": "object", "additionalProperties": False,
        "required": ["schema", "origin", "split_seed", "implementation_author_ids",
                     "reviewer_ids", "attestation_sha256", "frozen_at", "arms",
                     "token_budgets", "scenarios"],
        "properties": {
            "schema": {"const": SCHEMA}, "origin": {"enum": sorted(ORIGINS)},
            "split_seed": {"type": "integer"},
            "implementation_author_ids": {"type": "array", "minItems": 1,
                                          "uniqueItems": True, "items": identity},
            "reviewer_ids": {"type": "array", "minItems": 2, "uniqueItems": True,
                             "items": identity},
            "attestation_sha256": digest,
            "frozen_at": {"type": "string", "format": "date-time"},
            "arms": {"const": list(ARMS)}, "token_budgets": {"const": list(BUDGETS)},
            "scenarios": {"type": "array", "minItems": 400, "maxItems": 400,
                          "items": scenario},
        },
    }


def family_splits(families: list[str], seed: int) -> dict[str, str]:
    ordered = sorted(families, key=lambda family: hashlib.sha256(
        f"{seed}:{family}".encode("utf-8")).hexdigest())
    return {family: ("development" if index < 8 else "validation" if index < 16 else "held_out")
            for index, family in enumerate(ordered)}


def _ids(value, field: str, minimum: int = 0) -> list[str]:
    if (not isinstance(value, list) or len(value) < minimum
            or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{field} requires distinct bounded identifiers")
    return value


def validate_corpus(document: dict, *, require_independent: bool = True,
                    attestation_path: Optional[Path] = None) -> dict:
    """Check structure and provenance declarations; declarations cannot prove authorship."""
    if not isinstance(document, dict):
        raise ValueError("corpus must be an object")
    shape = schema()
    if set(document) != set(shape["required"]) or document.get("schema") != SCHEMA:
        raise ValueError("corpus fields/schema must match the versioned contract")
    origin = document["origin"]
    if origin not in ORIGINS:
        raise ValueError("unknown corpus origin")
    if require_independent and origin != "independent_human":
        raise ValueError("synthetic or implementation-authored fixtures are not independent evidence")
    if type(document["split_seed"]) is not int:
        raise ValueError("split_seed must be an integer")
    implementation = set(_ids(document["implementation_author_ids"], "implementation authors", 1))
    reviewers = set(_ids(document["reviewer_ids"], "reviewers", 2))
    if require_independent and reviewers & implementation:
        raise ValueError("independent reviewers cannot be implementation authors")
    if not isinstance(document["attestation_sha256"], str) or not _SHA.fullmatch(
        document["attestation_sha256"]
    ):
        raise ValueError("authorship attestation requires a SHA-256 identity")
    from datetime import datetime

    try:
        frozen = datetime.fromisoformat(document["frozen_at"].replace("Z", "+00:00"))
        if frozen.tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("frozen_at must be a timezone-aware ISO timestamp") from exc
    if document["arms"] != list(ARMS) or document["token_budgets"] != list(BUDGETS):
        raise ValueError("the matched matrix requires exactly five arms and three budgets")
    scenarios = document["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != 400:
        raise ValueError("the acceptance corpus requires exactly 400 scenarios")
    required = set(shape["properties"]["scenarios"]["items"]["required"])
    families = defaultdict(list)
    seen = set()
    for row in scenarios:
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("scenario fields must match the versioned contract")
        for key in ("id", "family_id"):
            if not isinstance(row[key], str) or not _ID.fullmatch(row[key]):
                raise ValueError(f"invalid scenario {key}")
        if row["id"] in seen:
            raise ValueError("duplicate scenario id")
        seen.add(row["id"])
        if row["category"] not in CATEGORIES or row["split"] not in SPLITS:
            raise ValueError("unknown scenario category/split")
        if row["origin"] != origin:
            raise ValueError("scenario provenance must agree; generated fixtures cannot be relabeled")
        authors = set(_ids(row["author_ids"], "scenario authors", 1))
        if require_independent and (authors & implementation or authors & reviewers):
            raise ValueError("task authors must be separate from implementation and review authors")
        for key in ("source_sha256", "oracle_sha256"):
            if not isinstance(row[key], str) or not _SHA.fullmatch(row[key]):
                raise ValueError(f"scenario {key} must bind frozen input bytes")
        _ids(row["required_evidence_ids"], "required evidence")
        if row["category"] not in {"unsupported_questions", "poisoning"} and not row["required_evidence_ids"]:
            raise ValueError("answerable task categories require labeled evidence")
        families[row["family_id"]].append(row)
    if len(families) != 40:
        raise ValueError("the corpus requires exactly 40 repository families")
    expected_splits = family_splits(list(families), document["split_seed"])
    for family, rows in families.items():
        if Counter(row["category"] for row in rows) != Counter(CATEGORIES):
            raise ValueError("each family must contain one scenario per category")
        if any(row["split"] != expected_splits[family] for row in rows):
            raise ValueError("family leakage or split assignment differs from the frozen seed")
    attestation_matches = False
    if attestation_path is not None:
        attestation_matches = sha256_file(attestation_path) == document["attestation_sha256"]
        if not attestation_matches:
            raise ValueError("authorship attestation bytes do not match")
    if require_independent and not attestation_matches:
        raise ValueError("independent-candidate validation requires the bound attestation file")
    return {"schema": SCHEMA, "structurally_valid": True,
            "scenario_count": 400, "family_count": 40,
            "splits": dict(Counter(row["split"] for row in scenarios)),
            "matrix_cells_per_scenario": 15, "origin": origin,
            "authorship_status": "attested_unverified" if attestation_matches else "unverified",
            "independently_authored_verified": False,
            "publication_ready": False,
            "manifest_sha256": hashlib.sha256(canonical_json(document).encode()).hexdigest()}


def validate_matched_bindings(bindings: list[dict]) -> dict:
    """Preflight an exact 5x3 run manifest; no model calls or budget authorization."""
    if not isinstance(bindings, list) or len(bindings) != len(ARMS) * len(BUDGETS):
        raise ValueError("expected exactly 15 matched run bindings")
    common = {"corpus_sha256", "prompt_sha256", "reader_id", "reader_revision",
              "tokenizer_id", "tokenizer_revision", "max_input_tokens", "max_output_tokens",
              "embedding_id", "embedding_revision", "embedding_semantic",
              "source_visibility_sha256", "history_overflow_policy", "seed"}
    expected = {(arm, budget) for arm in ARMS for budget in BUDGETS}
    observed = set()
    reference = None
    for row in bindings:
        if not isinstance(row, dict) or set(row) != common | {"arm", "token_budget"}:
            raise ValueError("run binding fields must match the matrix contract")
        cell = (row["arm"], row["token_budget"])
        if cell not in expected or cell in observed:
            raise ValueError("duplicate or unknown arm/budget cell")
        observed.add(cell)
        values = {key: row[key] for key in common}
        for key in ("corpus_sha256", "prompt_sha256", "source_visibility_sha256"):
            if not isinstance(values[key], str) or not _SHA.fullmatch(values[key]):
                raise ValueError("run inputs require frozen SHA-256 identities")
        for key in ("reader_id", "reader_revision", "tokenizer_id", "tokenizer_revision",
                    "embedding_id", "embedding_revision"):
            if not isinstance(values[key], str) or not values[key].strip():
                raise ValueError("reader/tokenizer identities and revisions are required")
        for key in ("max_input_tokens", "max_output_tokens"):
            minimum = max(BUDGETS) if key == "max_input_tokens" else 1
            if type(values[key]) is not int or values[key] < minimum:
                raise ValueError("shared resource ceilings must accommodate declared budgets")
        if values["embedding_semantic"] is not True:
            raise ValueError("hashing is not a semantic dense baseline")
        if values["history_overflow_policy"] != "fail_preflight":
            raise ValueError("full-history overflow must fail preflight, never silently truncate")
        if type(values["seed"]) is not int:
            raise ValueError("seed must be an integer")
        if reference is not None and reference != values:
            raise ValueError("arms must share prompts, sources, reader and resource ceilings")
        reference = values
    return {"matched_cells": 15, "model_calls": 0, "paid_run_authorized": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--fixture", action="store_true", help="structural check; never independent evidence")
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args(argv)
    if args.corpus is None:
        result = schema()
    else:
        result = validate_corpus(json.loads(args.corpus.read_text(encoding="utf-8")),
                                 require_independent=not args.fixture,
                                 attestation_path=args.attestation)
    if args.bindings is not None:
        result["bindings"] = validate_matched_bindings(
            json.loads(args.bindings.read_text(encoding="utf-8")))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
