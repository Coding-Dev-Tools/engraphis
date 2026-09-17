"""Focused tests for the executable local user-journey evidence runner."""
from __future__ import annotations

import copy
import json

import eval.user_journeys as user_journeys
from eval.user_journeys import (
    AVAILABLE_JOURNEYS,
    EVIDENCE_KIND,
    SCHEMA,
    _core_context_budget_fallback,
    run_journey,
    run_journeys,
    verify_envelope,
)
from engraphis.service import MemoryService


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


def test_mcp_core_floor_fallback_records_the_optional_dependency_boundary():
    service = MemoryService.create(":memory:", graph_extractor="none")
    try:
        observation = _core_context_budget_fallback(service)
    finally:
        service.close()

    assert all(observation.checks.values())
    assert observation.counts["mcp_tools"] == 0
    assert observation.counts["core_context_tokens"] <= 12


def test_mcp_fallback_only_applies_when_optional_server_is_absent(monkeypatch):
    monkeypatch.setattr(user_journeys, "_load_mcp_server", lambda: None)

    outcome = user_journeys.run_journey("mcp_context_budget")

    assert outcome["status"] == "passed"
    assert outcome["counts"]["mcp_tools"] == 0


def test_mcp_runtime_system_exit_is_reported_instead_of_falling_back(monkeypatch):
    class BrokenMcp:
        _service = None

        @staticmethod
        def set_service(service):
            BrokenMcp._service = service

        @staticmethod
        def engraphis_remember(*args, **kwargs):
            raise SystemExit("wrapper failure")

    monkeypatch.setattr(user_journeys, "_load_mcp_server", lambda: BrokenMcp)

    outcome = user_journeys.run_journey("mcp_context_budget")

    assert outcome["status"] == "failed"
    assert outcome["error_type"] == "SystemExit"
