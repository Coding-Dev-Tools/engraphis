from types import SimpleNamespace

import pytest

from eval.campaign_api import (
    CampaignAPIError,
    CampaignUncertainCall,
    LunaResponsesClient,
    MODEL,
    estimate_cost_micros,
)
from eval.campaign_ledger import BudgetApproval, CampaignBinding, CampaignLedger


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeOAuthTransport(FakeTransport):
    identity = "codex_oauth"
    billing_basis = "subscription_usage_api_price_proxy_not_invoice"


def _client(tmp_path, transport, *, max_calls=4, max_cost_micros=1_000_000):
    binding = CampaignBinding(
        campaign_id="campaign-api-test", model=MODEL, reasoning_effort="medium",
        dataset_sha256="a" * 64, config_sha256="b" * 64,
        repo_revision="c" * 40, pins_sha256="d" * 64,
    )
    approval = BudgetApproval.create(
        max_calls=max_calls, max_cost_micros=max_cost_micros,
    )
    ledger = CampaignLedger(tmp_path / "calls.jsonl", binding, approval)
    return LunaResponsesClient(ledger, transport=transport), ledger


def _response(*, model=MODEL, output="ok", input_tokens=10, output_tokens=2,
              benchmark_provenance=None):
    value = SimpleNamespace(
        id="resp-1",
        model=model,
        output_text=output,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )
    if benchmark_provenance is not None:
        value.benchmark_provenance = benchmark_provenance
    return value


def test_responses_payload_is_exact_and_completed_call_is_resumed(tmp_path):
    transport = FakeTransport(_response())
    client, ledger = _client(tmp_path, transport)
    first = client.complete(
        call_id="reader-a", kind="reader", input="read this",
        instructions="return JSON", text={"format": {"type": "json_object"}},
        max_output_tokens=32,
    )
    assert first.text == "ok"
    assert first.provenance["transport"] == "responses"
    assert first.usage.worst_case_cost_micros >= first.usage.cost_micros
    assert len(transport.calls) == 1
    payload = transport.calls[0]
    assert payload["model"] == MODEL
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["store"] is False
    assert payload["tools"] == []
    assert payload["instructions"] == "return JSON"
    assert payload["text"]["format"]["type"] == "json_object"

    resumed = client.complete(
        call_id="reader-a", kind="reader", input="read this",
        instructions="return JSON", text={"format": {"type": "json_object"}},
        max_output_tokens=32,
    )
    assert resumed.text == "ok"
    assert resumed.provenance["resumed"] is True
    assert len(transport.calls) == 1
    assert ledger.lookup("reader-a").status == "completed"


def test_instructions_and_schema_are_bound_into_the_reservation(tmp_path):
    transport = FakeTransport(_response(input_tokens=1, output_tokens=1))
    client, ledger = _client(tmp_path, transport)
    client.complete(
        call_id="reader-a", kind="reader", input="x", input_tokens=0,
        instructions="long instructions", text={"format": {"type": "json_schema", "schema": {"x": "y"}}},
        max_output_tokens=8,
    )
    reservation = ledger.lookup("reader-a")
    assert reservation.estimated_cost_micros > 0
    assert reservation.request_sha256


def test_cache_write_reservation_uses_one_125x_input_charge():
    approval = BudgetApproval.create(max_calls=1, max_cost_micros=1_000_000)
    assert estimate_cost_micros(
        input_tokens=100, cache_write_tokens=100, output_tokens=0, approval=approval,
    ) == 25


def test_transport_error_is_uncertain_and_never_retried(tmp_path):
    transport = FakeTransport(error=RuntimeError("provider secret"))
    client, ledger = _client(tmp_path, transport)
    with pytest.raises(CampaignUncertainCall):
        client.complete(call_id="reader-a", kind="reader", input="x", max_output_tokens=8)
    assert ledger.lookup("reader-a").status == "uncertain"
    with pytest.raises(CampaignUncertainCall):
        client.complete(call_id="reader-a", kind="reader", input="x", max_output_tokens=8)
    assert len(transport.calls) == 1


def test_model_mismatch_is_terminal_and_not_retried(tmp_path):
    transport = FakeTransport(_response(model="other-model"))
    client, ledger = _client(tmp_path, transport)
    with pytest.raises(CampaignAPIError, match="different model"):
        client.complete(call_id="reader-a", kind="reader", input="x", max_output_tokens=8)
    assert ledger.lookup("reader-a").status == "failed"
    with pytest.raises(CampaignAPIError, match="terminally failed"):
        client.complete(call_id="reader-a", kind="reader", input="x", max_output_tokens=8)
    assert len(transport.calls) == 1


def test_campaign_requires_an_explicit_transport(tmp_path):
    _, ledger = _client(tmp_path, FakeTransport(_response()))
    with pytest.raises(CampaignAPIError, match="injected explicitly"):
        LunaResponsesClient(ledger)


def test_oauth_provenance_and_accounting_survive_resume(tmp_path):
    transport = FakeOAuthTransport(_response(benchmark_provenance={
        "transport": "codex_oauth",
        "billing_basis": "subscription_usage_api_price_proxy_not_invoice",
        "model_verification": "native_thread_start_and_no_model_reroute",
        "automatic_retries": 0,
        "requested_model": MODEL,
        "effective_model": MODEL,
        "reasoning_effort": "medium",
    }))
    client, ledger = _client(tmp_path, transport)
    first = client.complete(call_id="reader-oauth", kind="reader", input="x", max_output_tokens=8)
    assert first.usage.transport_identity == "codex_oauth"
    assert first.usage.billing_basis == "subscription_usage_api_price_proxy_not_invoice"
    journal = ledger.lookup("reader-oauth")
    assert journal.usage["transport_identity"] == "codex_oauth"
    assert journal.usage["billing_basis"] == "subscription_usage_api_price_proxy_not_invoice"
    assert transport.calls[0]["benchmark_call_id"] == "reader-oauth"
    assert transport.calls[0]["benchmark_request_sha256"] == journal.request_sha256
    resumed = client.complete(call_id="reader-oauth", kind="reader", input="x", max_output_tokens=8)
    assert resumed.provenance["transport"] == "codex_oauth"
    assert len(transport.calls) == 1
