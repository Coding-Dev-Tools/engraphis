"""Malformed provider accounting cannot become observed zero-cost usage."""

from contextlib import contextmanager
import hashlib
import json
import math
from types import SimpleNamespace

import pytest

from eval import benchmark_campaign as campaign
from eval.campaign_api import TokenUsage


CELL = {"scenario_id": "fixture-a", "arm": "hybrid", "token_budget": 512, "repetition": 0}


def valid_usage(**overrides):
    value = {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 12,
        "reasoning_output_tokens": 4,
        "total_tokens": 112,
        "latency_ms": 4.5,
        "cost_micros": 7,
        "worst_case_cost_micros": 19,
        # This is a reservation assumption and can exceed observed input.
        "cache_write_tokens_assumed": 140,
        "token_counter": "engraphis.utf8bytes.v1",
        "transport_identity": "codex_oauth",
        "billing_basis": campaign.OAUTH_BILLING_BASIS,
    }
    value.update(overrides)
    return value


def error_row(**extra):
    return {
        **CELL,
        "status": "error",
        "task_success": None,
        "critical_violations": [],
        **extra,
    }


def one_cell_manifest():
    value = {"stages": {"development_pilot": {
        "split": "development", "scenario_ids": [CELL["scenario_id"]], "arms": [CELL["arm"]],
        "repetitions": 1, "token_budgets": [CELL["token_budget"]], "max_reader_turns": 1,
        "max_peer_internal_calls_per_attempt": 1, "max_input_tokens": 100,
        "max_output_tokens": 10,
    }}}
    value["binding_sha256"] = campaign.digest(value)
    return value


def test_every_real_token_usage_field_is_required():
    usage = TokenUsage(
        input_tokens=100, cached_input_tokens=20, output_tokens=12, reasoning_output_tokens=4,
        total_tokens=112, latency_ms=4.5, cost_micros=7, worst_case_cost_micros=19,
        cache_write_tokens_assumed=140,
    ).as_dict()
    campaign.validate_row(error_row(provider_usage=[usage]), CELL)
    for field in usage:
        partial = dict(usage)
        partial.pop(field)
        with pytest.raises(ValueError, match="missing"):
            campaign.validate_row(error_row(provider_usage=[partial]), CELL)


@pytest.mark.parametrize("changes", [
    {"input_tokens": True}, {"cost_micros": 1.0}, {"total_tokens": "112"},
    {"cached_input_tokens": 101}, {"reasoning_output_tokens": 13}, {"total_tokens": 111},
    {"worst_case_cost_micros": -1}, {"cache_write_tokens_assumed": None},
    {"billing_basis": {}}, {"token_counter": ""}, {"transport_identity": False},
])
def test_malformed_usage_fields_are_rejected(changes):
    with pytest.raises(ValueError):
        campaign.validate_row(error_row(provider_usage=[valid_usage(**changes)]), CELL)


@pytest.mark.parametrize("reader", ["execute", "summarize"])
def test_checksummed_malformed_checkpoint_is_rejected_before_publication(tmp_path, reader):
    manifest = one_cell_manifest()
    recorded = error_row(
        status="complete", task_success=True, family_id="family-a", category="corrections",
        provider_usage=[{}], provider_usage_attempted=1,
    )
    directory = tmp_path / "development_pilot"
    directory.mkdir()
    campaign._save_new(directory / (campaign.digest(CELL) + ".json"), {
        "binding_sha256": manifest["binding_sha256"], "cell": CELL,
        "row": recorded, "row_sha256": campaign.digest(recorded),
    })
    with pytest.raises(ValueError, match="missing"):
        if reader == "execute":
            campaign.execute(
                manifest, "development_pilot", tmp_path, None, None,
                attempt_runner=lambda *_: pytest.fail("malformed checkpoint triggered replay"),
            )
        else:
            campaign.summarize(manifest, "development_pilot", tmp_path)


def test_explicit_zero_invocations_cannot_be_scored_complete():
    recorded = error_row(
        status="complete", task_success=True, provider_usage=[], provider_usage_attempted=0,
        provider_usage_observed=0, provider_usage_missing=0, provider_usage_status="not_attempted",
    )
    with pytest.raises(ValueError, match="zero provider invocations"):
        campaign.validate_row(recorded, CELL)
    with pytest.raises(ValueError, match="zero provider invocations"):
        campaign._provider_usage_summary([recorded])


@pytest.mark.parametrize("extra", [
    {}, {"provider_usage": {}}, {"provider_usage_attempted": False},
    {"provider_usage": {}, "provider_usage_attempted": 0},
])
def test_unusable_error_accounting_does_not_invent_known_zero_invocations(extra):
    recorded = campaign._attempt_error_row(CELL, ValueError("invalid row"), error_row(**extra))
    campaign.validate_row(recorded, CELL)
    assert "provider_usage_attempted" not in recorded
    assert campaign._provider_usage_summary([recorded])["status"] == "missing"


def test_finite_integer_latency_overflow_is_rejected_during_aggregation():
    with pytest.raises(ValueError, match="aggregate"):
        campaign._provider_usage_summary([{
            "provider_usage": [valid_usage(latency_ms=10 ** 308)] * 2,
        }])


def test_token_usage_shape_requires_complete_finite_entry_and_preserves_legacy_missing():
    with pytest.raises(ValueError, match="missing"):
        campaign.validate_row(error_row(provider_usage=[{}], provider_usage_attempted=1), CELL)
    with pytest.raises(ValueError, match="non-negative"):
        campaign.validate_row(
            error_row(provider_usage=[valid_usage(cost_micros=-1)], provider_usage_attempted=1), CELL
        )
    with pytest.raises(ValueError, match="finite"):
        campaign.validate_row(
            error_row(provider_usage=[valid_usage(latency_ms=math.inf)], provider_usage_attempted=1), CELL
        )
    with pytest.raises(ValueError, match="finite"):
        campaign.validate_row(
            error_row(provider_usage=[valid_usage(latency_ms=10**10000)], provider_usage_attempted=1), CELL
        )
    with pytest.raises(ValueError, match="below observed"):
        campaign.validate_row(
            error_row(provider_usage=[valid_usage()], provider_usage_attempted=0), CELL
        )
    with pytest.raises(ValueError, match="provider_usage_observed"):
        campaign.validate_row(
            error_row(provider_usage=[valid_usage()], provider_usage_attempted=1,
                      provider_usage_observed=True), CELL
        )
    # Legacy rows with no accounting remain explicitly unknown.
    campaign.validate_row(error_row(), CELL)
    assert campaign._provider_usage_summary([error_row()])["status"] == "missing"
    assert campaign._provider_usage_summary(
        [error_row(provider_usage=None)]
    )["status"] == "missing"
    legacy_missing = error_row(provider_usage=[], provider_usage_status="missing")
    campaign.validate_row(legacy_missing, CELL)
    assert campaign._provider_usage_summary([legacy_missing])["status"] == "missing"
    assert campaign._provider_usage_summary(
        [error_row(provider_usage=[], provider_usage_attempted=0)]
    )["status"] == "not_attempted"


def test_summary_counts_only_valid_entries_and_sums_complete_schema():
    usage = valid_usage(cost_micros=97, worst_case_cost_micros=10)
    result = campaign._provider_usage_summary([
        {"status": "complete", "provider_usage": [usage], "provider_usage_attempted": 1},
    ])
    assert result["status"] == "complete"
    assert result["calls_observed"] == 1
    assert result["provider_usage_observed"] == 1
    assert result["provider_usage_missing"] == 0
    assert result["cost_micros"] == 97
    assert "worst_case_cost_micros" not in result
    assert "cache_write_tokens_assumed" not in result
    with pytest.raises(ValueError, match="missing"):
        campaign._provider_usage_summary([
            {"status": "complete", "provider_usage": [{"input_tokens": 1}],
             "provider_usage_attempted": 1},
        ])
    with pytest.raises(ValueError, match="non-negative"):
        campaign._provider_usage_summary([
            {"status": "error", "provider_usage": [valid_usage(output_tokens=-1)],
             "provider_usage_attempted": 1},
        ])
    with pytest.raises(ValueError, match="finite"):
        campaign._provider_usage_summary([
            {"status": "error", "provider_usage": [valid_usage(latency_ms=float("nan"))],
             "provider_usage_attempted": 1},
        ])
    with pytest.raises(ValueError, match="aggregate"):
        campaign._provider_usage_summary([
            {"status": "complete", "provider_usage": [
                valid_usage(latency_ms=1e308), valid_usage(latency_ms=1e308),
            ], "provider_usage_attempted": 2},
        ])


def test_usage_state_and_terminal_error_filter_malformed_entries_to_missing():
    usage = valid_usage()
    observed, missing, status = campaign._usage_state([usage, {}], 2)
    assert (observed, missing, status) == (1, 1, "partial")
    assert campaign._usage_state([{}], 0) == (0, 1, "missing")
    typed = campaign._AttemptExecutionError(
        ValueError("post-response parse failure"), usage_rows=[{}], provider_usage_attempted=0,
    )
    assert typed.provider_usage == []
    assert typed.provider_usage_attempted == 1
    assert typed.provider_usage_observed == 0
    assert typed.provider_usage_missing == 1
    row = campaign._attempt_error_row(
        CELL,
        ValueError("post-response parse failure"),
        error_row(provider_usage=[{}], provider_usage_attempted=1),
    )
    assert row["provider_usage"] == []
    assert row["provider_usage_attempted"] == 1
    assert row["provider_usage_observed"] == 0
    assert row["provider_usage_missing"] == 1
    assert row["provider_usage_status"] == "missing"
    campaign.validate_row(row, CELL)


def test_execute_sanitizes_malformed_runner_row_without_replay(tmp_path):
    manifest = one_cell_manifest()
    calls = []
    def runner(_manifest, _stage, cell, *_args):
        calls.append(cell)
        return error_row(provider_usage=[{}], provider_usage_attempted=1)

    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=runner)
    assert len(calls) == 1
    assert summary["provider_usage"]["status"] == "missing"
    assert summary["provider_usage"]["provider_usage_attempted"] == 1
    assert summary["provider_usage"]["provider_usage_observed"] == 0
    checkpoint = next((tmp_path / "development_pilot").glob("*.json"))
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))["row"]
    assert saved["provider_usage"] == []
    assert saved["provider_usage_missing"] == 1
    resumed = campaign.execute(
        manifest, "development_pilot", tmp_path, None, None,
        attempt_runner=lambda *_args: pytest.fail("terminal malformed attempt replayed"),
    )
    assert resumed["provider_usage"]["status"] == "missing"
    assert len(calls) == 1



def test_fresh_run_attempt_malformed_usage_keeps_typed_missingness(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    oracle = tmp_path / "oracle.py"
    oracle.write_text("# oracle", encoding="utf-8")
    task = SimpleNamespace(
        prompt="Fix the function.", target_files=("service.py",),
        required_evidence_ids=(), forbidden_evidence_ids=(), untrusted_evidence_ids=(),
        answer_tokens=(), answerable=True, valid_at=None, known_at=None,
    )
    scenario = SimpleNamespace(
        id="fixture-a", source_path=source, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        oracle_path=oracle, oracle_sha256=hashlib.sha256(oracle.read_bytes()).hexdigest(),
        task=task, family_id="family-a", category="corrections", operations=(),
    )

    @contextmanager
    def workspace(_scenario, target):
        target.mkdir()
        (target / "service.py").write_text("def result(): return 0", encoding="utf-8")
        yield target

    monkeypatch.setattr(campaign, "scenario_workspace", workspace)
    manifest = {"stages": {"development_pilot": {
        "max_reader_turns": 1, "max_input_tokens": 10000, "max_output_tokens": 10,
    }}, "binding_sha256": "a" * 64}
    cell = {"scenario_id": "fixture-a", "arm": "no_memory", "token_budget": 512, "repetition": 0}

    class Client:
        def complete(self, **_kwargs):
            return SimpleNamespace(
                text="{malformed",
                usage=SimpleNamespace(as_dict=lambda: {}),
            )

    with pytest.raises(campaign._AttemptExecutionError) as caught:
        campaign.run_attempt(
            manifest, "development_pilot", cell, SimpleNamespace(get=lambda _id: scenario), Client(),
            oracle=lambda *_args: {"passed": True, "timed_out": False},
        )
    assert caught.value.provider_usage == []
    assert caught.value.provider_usage_attempted == 1
    assert caught.value.provider_usage_observed == 0
    assert caught.value.provider_usage_missing == 1
    row = campaign._attempt_error_row(cell, caught.value)
    assert row["provider_usage"] == []
    assert row["provider_usage_attempted"] == 1
    assert row["provider_usage_missing"] == 1
    campaign.validate_row(row, cell)
