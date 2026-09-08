from copy import deepcopy
import hashlib

import pytest

from eval.coding_acceptance import (
    ARMS, BUDGETS, CATEGORIES, SCHEMA, family_splits, schema,
    validate_corpus, validate_matched_bindings,
)


def _fixture():
    """Intentionally generated structural data; never independent task evidence."""
    families = [f"family-{i:02d}" for i in range(40)]
    splits = family_splits(families, 42)
    return {
        "schema": SCHEMA, "origin": "synthetic_fixture", "split_seed": 42,
        "implementation_author_ids": ["implementation"], "reviewer_ids": ["reviewer-a", "reviewer-b"],
        "attestation_sha256": "a" * 64, "frozen_at": "2026-09-05T00:00:00Z",
        "arms": list(ARMS), "token_budgets": list(BUDGETS),
        "scenarios": [{"id": f"{family}:{category}", "family_id": family,
                       "category": category, "split": splits[family], "origin": "synthetic_fixture",
                       "author_ids": ["fixture-generator"], "source_sha256": "b" * 64,
                       "oracle_sha256": "c" * 64, "required_evidence_ids": ["evidence-1"]}
                      for family in families for category in CATEGORIES],
    }


def test_corpus_schema_and_exact_family_split_protocol():
    result = validate_corpus(_fixture(), require_independent=False)
    assert result["splits"] == {"development": 80, "validation": 80, "held_out": 240}
    assert result["scenario_count"] == 400 and result["family_count"] == 40
    assert result["matrix_cells_per_scenario"] == 15
    assert result["independently_authored_verified"] is False
    assert result["publication_ready"] is False
    assert schema()["properties"]["scenarios"]["minItems"] == 400


def test_generated_fixture_cannot_be_called_independently_authored():
    with pytest.raises(ValueError, match="not independent"):
        validate_corpus(_fixture())
    changed = _fixture()
    changed["origin"] = "independent_human"
    with pytest.raises(ValueError, match="provenance"):
        validate_corpus(changed)


@pytest.mark.parametrize("damage", ["duplicate", "family_leak", "missing", "budget", "category", "hash"])
def test_invalid_corpus_is_rejected(damage):
    document = _fixture()
    row = document["scenarios"][0]
    if damage == "duplicate":
        document["scenarios"][1]["id"] = row["id"]
    elif damage == "family_leak":
        row["split"] = "held_out" if row["split"] != "held_out" else "development"
    elif damage == "missing":
        document["scenarios"].pop()
    elif damage == "budget":
        document["token_budgets"] = [512, 1024, 4096]
    elif damage == "category":
        row["category"] = document["scenarios"][1]["category"]
    else:
        row["oracle_sha256"] = "unfrozen"
    with pytest.raises(ValueError):
        validate_corpus(document, require_independent=False)


def test_bound_attestation_is_a_declaration_not_automatic_authorship_proof(tmp_path):
    document = _fixture()
    document["origin"] = "independent_human"
    for row in document["scenarios"]:
        row["origin"] = "independent_human"
        row["author_ids"] = ["declared-independent-author"]
    evidence = tmp_path / "declaration.txt"
    evidence.write_text("Test-only authorship declaration, not actual human tasks.", encoding="utf-8")
    document["attestation_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="attestation file"):
        validate_corpus(document)
    result = validate_corpus(document, attestation_path=evidence)
    assert result["authorship_status"] == "attested_unverified"
    assert result["independently_authored_verified"] is False
    document["scenarios"][0]["author_ids"] = ["implementation"]
    with pytest.raises(ValueError, match="separate"):
        validate_corpus(document, attestation_path=evidence)


def _bindings():
    common = {"corpus_sha256": "a" * 64, "prompt_sha256": "b" * 64,
              "reader_id": "fixed-reader", "reader_revision": "revision-1",
              "tokenizer_id": "fixed-tokenizer", "tokenizer_revision": "revision-1",
              "embedding_id": "local-model", "embedding_revision": "c" * 64,
              "embedding_semantic": True, "history_overflow_policy": "fail_preflight",
              "max_input_tokens": 128000, "max_output_tokens": 8192,
              "source_visibility_sha256": "d" * 64, "seed": 42}
    return [{**deepcopy(common), "arm": arm, "token_budget": budget}
            for arm in ARMS for budget in BUDGETS]


def test_matched_matrix_shares_exact_inputs_and_does_not_authorize_calls():
    assert validate_matched_bindings(_bindings()) == {
        "matched_cells": 15, "model_calls": 0, "paid_run_authorized": False,
    }


@pytest.mark.parametrize("field,value", [
    ("prompt_sha256", "e" * 64), ("max_output_tokens", 16384),
    ("embedding_semantic", False), ("history_overflow_policy", "truncate"),
    ("arm", "no_memory"),
])
def test_unmatched_or_misleading_run_matrix_is_rejected(field, value):
    rows = _bindings()
    rows[-1][field] = value
    with pytest.raises(ValueError):
        validate_matched_bindings(rows)
