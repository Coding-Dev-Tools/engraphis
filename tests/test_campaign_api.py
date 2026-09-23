from types import SimpleNamespace

import pytest

from eval.campaign_api import (
    CampaignAPIError,
    CampaignUncertainCall,
    LunaResponsesClient,
    MODEL,
    estimate_cost_micros,
)
from eval.campaign_ledger import (
    BudgetApproval,
    BudgetExceeded,
    CampaignBinding,
    CampaignLedger,
)


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


def test_partial_cache_write_prices_uncovered_input_and_rounds_once():
    approval = BudgetApproval.create(max_calls=1, max_cost_micros=1_000_000)

    assert estimate_cost_micros(
        input_tokens=0, cache_write_tokens=0, output_tokens=0, approval=approval,
    ) == 0
    assert estimate_cost_micros(
        input_tokens=100, cache_write_tokens=0, output_tokens=0, approval=approval,
    ) == 20
    # 50 ordinary tokens cost 10 micros and 50 cache-write tokens cost 12.5;
    # the uncovered ordinary portion must not disappear behind max(20, 13).
    assert estimate_cost_micros(
        input_tokens=100, cache_write_tokens=50, output_tokens=0, approval=approval,
    ) == 23
    assert estimate_cost_micros(
        input_tokens=1_000_000_000,
        cache_write_tokens=1_000_000_000,
        output_tokens=0,
        approval=approval,
    ) == 250_000_000
    # 0.25 + 1.2 micros is 1.45 micros; one final ceiling gives 2.
    assert estimate_cost_micros(
        input_tokens=1, cache_write_tokens=1, output_tokens=1, approval=approval,
    ) == 2
    assert estimate_cost_micros(
        input_tokens=100, cached_input_tokens=20, cache_write_tokens=30,
        output_tokens=0, approval=approval,
    ) == 18


def test_cache_pricing_honors_custom_rates_and_large_integer_counts():
    custom = BudgetApproval.create(
        max_calls=1,
        max_cost_micros=1_000_000,
        input_micros_per_million=100_000,
        cached_input_micros_per_million=200_000,
        cache_write_micros_per_million=300_000,
        output_micros_per_million=400_000,
    )
    # Five ordinary + two cached + three write + four output tokens = 3.4 micros.
    assert estimate_cost_micros(
        input_tokens=10, cached_input_tokens=2, cache_write_tokens=3,
        output_tokens=4, approval=custom,
    ) == 4

    huge = 2**53 + 123
    expected = (huge * 250_000 + 999_999) // 1_000_000
    assert estimate_cost_micros(
        input_tokens=huge, cache_write_tokens=huge, output_tokens=0,
        approval=BudgetApproval.create(max_calls=1, max_cost_micros=10**20),
    ) == expected


@pytest.mark.parametrize(
    "input_tokens,cached_input_tokens,cache_write_tokens",
    [(0, 0, 1), (100, 101, 0), (100, 51, 50), (100, 100, 1)],
)
def test_cache_pricing_rejects_overlapping_or_out_of_range_categories(
    input_tokens, cached_input_tokens, cache_write_tokens,
):
    approval = BudgetApproval.create(max_calls=1, max_cost_micros=1_000_000)
    with pytest.raises(CampaignAPIError, match="cannot exceed"):
        estimate_cost_micros(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=0,
            approval=approval,
        )


@pytest.mark.parametrize(
    "field,value",
    [("input_tokens", True), ("cached_input_tokens", 1.5),
     ("cache_write_tokens", "1"), ("output_tokens", False)],
)
def test_cache_pricing_rejects_invalid_category_types(field, value):
    values = {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
    }
    values[field] = value
    with pytest.raises(CampaignAPIError, match=field):
        estimate_cost_micros(**values)


def test_partial_cache_reservation_gate_rejects_before_transport_and_resumes_once(tmp_path):
    binding = CampaignBinding(
        campaign_id="campaign-api-pricing-gate", model=MODEL, reasoning_effort="medium",
        dataset_sha256="a" * 64, config_sha256="b" * 64,
        repo_revision="c" * 40, pins_sha256="d" * 64,
    )

    def make_client(path, ceiling, transport):
        approval = BudgetApproval.create(
            max_calls=1,
            max_cost_micros=ceiling,
            output_micros_per_million=0,
        )
        ledger = CampaignLedger(path, binding, approval)
        return LunaResponsesClient(ledger, transport=transport), ledger

    rejected_transport = FakeTransport(_response(input_tokens=100, output_tokens=0))
    rejected, rejected_ledger = make_client(tmp_path / "reject.jsonl", 22, rejected_transport)
    with pytest.raises(BudgetExceeded, match="cash ceiling"):
        rejected.complete(
            call_id="partial-gate", kind="reader", input="x", input_tokens=100,
            cache_write_tokens=50, max_output_tokens=1,
        )
    assert rejected_transport.calls == []
    assert rejected_ledger.lookup("partial-gate") is None

    transport = FakeTransport(_response(input_tokens=100, output_tokens=0))
    admitted, ledger = make_client(tmp_path / "admit.jsonl", 23, transport)
    first = admitted.complete(
        call_id="partial-gate", kind="reader", input="x", input_tokens=100,
        cache_write_tokens=50, max_output_tokens=1,
    )
    resumed = admitted.complete(
        call_id="partial-gate", kind="reader", input="x", input_tokens=100,
        cache_write_tokens=50, max_output_tokens=1,
    )
    assert first.text == resumed.text == "ok"
    assert ledger.lookup("partial-gate").estimated_cost_micros == 23
    assert resumed.provenance["resumed"] is True
    assert len(transport.calls) == 1


def test_default_cache_reservation_keeps_an_unverified_cached_hint_conservative(tmp_path):
    transport = FakeTransport(_response(input_tokens=10, output_tokens=2))
    client, ledger = _client(tmp_path, transport)
    client.complete(
        call_id="reader-cached", kind="reader", input="x", input_tokens=100,
        cached_input_tokens=50, max_output_tokens=8,
    )
    reservation = ledger.lookup("reader-cached")
    expected = estimate_cost_micros(
        input_tokens=100, cached_input_tokens=0, cache_write_tokens=100,
        output_tokens=8, approval=ledger.approval,
    )
    assert reservation.estimated_cost_micros == expected
    assert reservation.usage["cache_write_tokens_assumed"] == 100


def test_unverified_cached_input_cannot_underreserve_observed_ordinary_usage(tmp_path):
    transport = FakeTransport(_response(input_tokens=100, output_tokens=0))
    client, ledger = _client(tmp_path, transport)
    client.complete(
        call_id="uncached-after-hint", kind="reader", input="x", input_tokens=100,
        cached_input_tokens=50, cache_write_tokens=0, max_output_tokens=1,
    )
    reservation = ledger.lookup("uncached-after-hint")
    assert reservation.status == "completed"
    assert reservation.estimated_cost_micros >= 20
    assert reservation.usage["cached_input_tokens"] == 0
    assert len(transport.calls) == 1


@pytest.mark.parametrize("cached_rate,observed_cached", [(20_000, 0), (500_000, 100)])
def test_reservation_covers_known_usage_with_custom_input_rates(
    tmp_path, cached_rate, observed_cached,
):
    binding = CampaignBinding(
        campaign_id="campaign-api-custom-ceiling", model=MODEL, reasoning_effort="medium",
        dataset_sha256="a" * 64, config_sha256="b" * 64,
        repo_revision="c" * 40, pins_sha256="d" * 64,
    )
    approval = BudgetApproval.create(
        max_calls=1, max_cost_micros=1_000_000,
        cached_input_micros_per_million=cached_rate,
        cache_write_micros_per_million=0,
    )
    ledger = CampaignLedger(tmp_path / "custom.jsonl", binding, approval)
    response = _response(input_tokens=100, output_tokens=1)
    response.usage.input_tokens_details.cached_tokens = observed_cached
    transport = FakeTransport(response)
    client = LunaResponsesClient(ledger, transport=transport)
    result = client.complete(
        call_id="custom-rates", kind="reader", input="x", input_tokens=100,
        max_output_tokens=1,
    )
    assert ledger.lookup("custom-rates").status == "completed"
    assert result.usage.worst_case_cost_micros >= result.usage.cost_micros
    assert result.usage.cached_input_tokens == observed_cached
    assert len(transport.calls) == 1


@pytest.mark.parametrize("counts", [
    {"input_tokens": -1},
    {"input_tokens": 100, "cached_input_tokens": 51, "cache_write_tokens": 50},
    {"input_tokens": 100, "cached_input_tokens": 101},
])
def test_invalid_input_categories_stop_before_reservation_and_transport(tmp_path, counts):
    transport = FakeTransport(_response())
    client, ledger = _client(tmp_path, transport)
    with pytest.raises(CampaignAPIError):
        client.complete(
            call_id="invalid-counts", kind="reader", input="x", max_output_tokens=1,
            **counts,
        )
    assert transport.calls == []
    assert ledger.lookup("invalid-counts") is None


def test_partial_write_reservation_covers_cached_remainder_with_custom_rates(tmp_path):
    binding = CampaignBinding(
        campaign_id="campaign-api-mixed-ceiling", model=MODEL, reasoning_effort="medium",
        dataset_sha256="a" * 64, config_sha256="b" * 64,
        repo_revision="c" * 40, pins_sha256="d" * 64,
    )
    approval = BudgetApproval.create(
        max_calls=1, max_cost_micros=27,
        input_micros_per_million=100_000,
        cached_input_micros_per_million=200_000,
        cache_write_micros_per_million=300_000,
    )
    ledger = CampaignLedger(tmp_path / "mixed.jsonl", binding, approval)
    transport = FakeTransport(_response(input_tokens=100, output_tokens=1))
    client = LunaResponsesClient(ledger, transport=transport)
    client.complete(
        call_id="mixed-rates", kind="reader", input="x", input_tokens=100,
        cache_write_tokens=50, max_output_tokens=1,
    )
    # 50 written + 50 cached tokens can cost 25 micros; output adds 1.2.
    # All-ordinary input and all-cached input without writes would each miss it.
    assert ledger.lookup("mixed-rates").estimated_cost_micros == 27
    assert len(transport.calls) == 1


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
