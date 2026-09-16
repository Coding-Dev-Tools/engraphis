"""Focused tests for the executable local user-journey evidence runner."""
from __future__ import annotations

import copy
import json

from eval.user_journeys import (
    AVAILABLE_JOURNEYS,
    EVIDENCE_KIND,
    SCHEMA,
    run_journey,
    run_journeys,
    verify_envelope,
)


def test_all_user_journeys_execute_as_runtime_evidence():
    envelope = run_journeys()

    assert envelope["schema"] == SCHEMA
    assert verify_envelope(envelope)
    payload = envelope["payload"]
    assert payload["evidence_kind"] == EVIDENCE_KIND
    assert payload["journey_count"] == len(AVAILABLE_JOURNEYS) == 7
    assert payload["passed"] == 7
    assert payload["failed"] == 0
    assert [item["journey_id"] for item in payload["journeys"]] == list(
        AVAILABLE_JOURNEYS
    )
    for item in payload["journeys"]:
        assert item["status"] == "passed"
        assert item["checks_failed"] == 0
        assert item["checks_passed"] >= 1
        assert item["duration_ms"] >= 0
        assert all(isinstance(value, bool) for value in item["checks"].values())
        assert all(isinstance(value, int) and value >= 0 for value in item["counts"].values())

    # Public evidence is deliberately content-free. The runtime actions and
    # their counts are not allowed to turn into a memory export.
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert "mem_" not in serialized_payload
    assert "repo_" not in serialized_payload
    assert "workspace_id" not in serialized_payload
    assert "database" not in serialized_payload.lower()


def test_single_journey_selection_and_checksum_tamper_detection():
    envelope = run_journeys(["mcp_context_budget"])

    assert verify_envelope(envelope)
    assert envelope["payload"]["journey_count"] == 1
    assert envelope["payload"]["journeys"][0]["journey_id"] == "mcp_context_budget"

    tampered = copy.deepcopy(envelope)
    tampered["payload"]["journeys"][0]["duration_ms"] += 1
    assert not verify_envelope(tampered)


def test_single_outcome_is_safe_and_does_not_require_campaign_state():
    outcome = run_journey("index_repair")

    assert outcome["journey_id"] == "index_repair"
    assert outcome["evidence_kind"] == EVIDENCE_KIND
    assert outcome["status"] == "passed"
    assert outcome["counts"]["repaired"] == 1
    assert "error_message" not in outcome
