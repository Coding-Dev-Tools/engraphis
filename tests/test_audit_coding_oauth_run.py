import hashlib
import json

from scripts.audit_coding_oauth_run import (
    MODEL,
    REASONING_EFFORT,
    TRANSPORT,
    audit_run,
    canonical_json,
    sha256_text,
)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def make_fixture(tmp_path, *, oracle_timeout=False, request_mismatch=False):
    manifest_unsigned = {
        "schema": "engraphis-benchmark-campaign/v1",
        "campaign_id": "audit-test",
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "transport": TRANSPORT,
        "automatic_retries": 0,
        "repository_revision": "a" * 40,
        "pins_sha256": "b" * 64,
        "corpus": {"manifest_sha256": "c" * 64},
        "oauth": {
            "transport": TRANSPORT,
            "provider": "engraphis_benchmark_oauth",
            "requested_model": MODEL,
            "effective_model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "instruction_sha256": "d" * 64,
            "global_instruction_sha256": "d" * 64,
        },
        "stages": {
            "development_pilot": {
                "scenario_ids": ["scenario"],
                "arms": ["hybrid"],
                "token_budgets": [512],
                "repetitions": 1,
            }
        },
    }
    manifest = {**manifest_unsigned, "binding_sha256": digest(manifest_unsigned)}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")

    results = tmp_path / "results"
    checkpoint_dir = results / "development_pilot"
    checkpoint_dir.mkdir(parents=True)
    attempt_id = "attempt-test"
    call_id = f"{attempt_id}-reader-0"
    request_sha = "e" * 64
    response = '{"answer":"ok","citations":[]}'
    usage = {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 4,
        "reasoning_output_tokens": 1,
        "total_tokens": 14,
    }
    oracle = {
        "passed": True,
        "timed_out": oracle_timeout,
        "returncode": 0 if not oracle_timeout else 7,
        "stdout": "",
        "stderr": "",
    }
    row = {
        "scenario_id": "scenario",
        "family_id": "family",
        "category": "coding",
        "arm": "hybrid",
        "token_budget": 512,
        "repetition": 0,
        "attempt_id": attempt_id,
        "status": "complete",
        "task_success": True,
        "reader_calls": 1,
        "context_tokens": 4,
        "provider_usage": [usage],
        "private_responses": [{"answer": "ok", "citations": []}],
        "private_oracles": [oracle],
        "oracle_calls": 1,
        "critical_violations": [],
    }
    checkpoint = {
        "binding_sha256": manifest["binding_sha256"],
        "cell": {key: row[key] for key in ("scenario_id", "arm", "token_budget", "repetition")},
        "row": row,
        "row_sha256": digest(row),
    }
    (checkpoint_dir / "checkpoint.json").write_text(
        canonical_json(checkpoint) + "\n", encoding="utf-8"
    )

    spending = results / "spending"
    spending.mkdir()
    binding = {
        "campaign_id": "audit-test-development_pilot",
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "dataset_sha256": "c" * 64,
        "config_sha256": manifest["binding_sha256"],
        "repo_revision": "a" * 40,
        "pins_sha256": "b" * 64,
    }
    ledger_usage = {
        **usage,
        "latency_ms": 1.0,
        "cost_micros": 0,
        "billing_basis": "subscription_usage_api_price_proxy_not_invoice",
        "transport_identity": TRANSPORT,
    }
    ledger = [
        {"kind": "header", "schema_version": "engraphis-campaign-ledger/1", "binding": binding},
        {"kind": "reserved", "schema_version": "engraphis-campaign-ledger/1",
         "call_id": call_id, "call_kind": "reader", "request_sha256": request_sha},
        {"kind": "dispatched", "schema_version": "engraphis-campaign-ledger/1",
         "call_id": call_id, "call_kind": "reader"},
        {"kind": "completed", "schema_version": "engraphis-campaign-ledger/1",
         "call_id": call_id, "call_kind": "reader", "response": response,
         "response_sha256": sha256_text(response), "usage": ledger_usage},
    ]
    (spending / "development_pilot.jsonl").write_text(
        "".join(canonical_json(item) + "\n" for item in ledger), encoding="utf-8"
    )

    journal_dir = results / "oauth-transport" / "attempt-test"
    journal_dir.mkdir(parents=True)
    journal_request = "f" * 64 if request_mismatch else request_sha
    journal = [
        {"event": "dispatch", "call_id": call_id, "request_sha256": journal_request,
         "thread_id": "thread-test", "model": MODEL, "effort": REASONING_EFFORT,
         "instruction_sha256": "d" * 64},
        {"event": "turn_binding", "call_id": call_id, "request_sha256": journal_request,
         "thread_id": "thread-test", "turn_id": "turn-test"},
        {"event": "thread/started", "thread_id": "thread-test", "turn_id": "turn-test"},
        {"event": "turn/started", "thread_id": "thread-test", "turn_id": "turn-test"},
        {"event": "output", "text": response},
        {"event": "usage", "usage": {
            "inputTokens": 10, "cachedInputTokens": 2, "outputTokens": 4,
            "reasoningOutputTokens": 1, "totalTokens": 14,
        }},
        {"event": "turn/completed", "thread_id": "thread-test", "turn_id": "turn-test"},
    ]
    (journal_dir / "events.jsonl").write_text(
        "".join(canonical_json(item) + "\n" for item in journal), encoding="utf-8"
    )

    config = {"campaign_sha256": manifest["binding_sha256"], "stage": "development_pilot"}
    report = {
        "schema": "engraphis-benchmark/v2",
        "suite": {"name": "audit", "dataset": "audit.json", "sha256": "1" * 64, "sources": []},
        "system": {
            "git_commit": "2" * 40,
            "git_dirty": False,
            "dirty_state_sha256": "3" * 64,
            "config_sha256": digest(config),
        },
        "environment": {},
        "protocol": {
            "command": ["audit"],
            "config": config,
            "token_accounting": {
                "identity": "test",
                "revision": None,
                "scope": "test",
                "method": "test",
            },
            "n_total": 1,
            "n_scored": 1,
        },
        "privacy": {
            "raw_query_policy": "omitted",
            "raw_answer_policy": "omitted",
            "raw_context_policy": "omitted",
            "content_fingerprint_policy": "omitted",
        },
        "models": {"reader": {
            "effective_model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "transport": TRANSPORT,
            "instruction_sha256": "d" * 64,
        }},
        "metrics": {
            "campaign_sha256": manifest["binding_sha256"],
            "stage": "development_pilot",
            "status": "COMPLETE",
            "missing_attempts": 0,
            "critical_violations": 0,
        },
        "exclusions": [],
        "records": [{
            "question_id": "scenario:hybrid:512:0",
            "status": "complete",
            "task_success": True,
            "reader_calls": 1,
            "context_tokens": 4,
            "token_budget": 512,
        }],
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
    return manifest_path, report_path, results


def run_fixture(tmp_path, **kwargs):
    manifest, report, results = make_fixture(tmp_path, **kwargs)
    return audit_run(
        manifest_path=manifest,
        report_path=report,
        results=results,
        private_inventory=tmp_path / "private-inventory.json",
        output=tmp_path / "public-audit.json",
    )


def test_post_run_audit_binds_fake_journal_ledger_and_checkpoint(tmp_path):
    result = run_fixture(tmp_path)
    assert result["status"] == "COMPLETE"
    assert result["counts"]["completed_calls"] == 1
    assert result["counts"]["native_turns_complete"] == 1
    assert result["usage"]["total_tokens"] == 14
    public = (tmp_path / "public-audit.json").read_text(encoding="utf-8")
    assert "attempt-test" not in public
    assert "thread-test" not in public
    assert '"answer":"ok"' not in public
    assert str(tmp_path / "results") not in public
    private = json.loads((tmp_path / "private-inventory.json").read_text(encoding="utf-8"))
    assert private["journals"][0]["sha256"]
    assert private["checkpoints"][0]["sha256"]
    assert private["ledgers"][0]["sha256"]


def test_oracle_timeout_and_nonzero_exit_block_complete_audit(tmp_path):
    result = run_fixture(tmp_path, oracle_timeout=True)
    assert result["status"] == "BLOCKED"
    assert result["counts"]["oracle_timeouts"] == 1
    assert result["counts"]["oracle_nonzero"] == 1
    assert "oracle_timeout" in result["issues"]
    assert "oracle_nonzero_exit" in result["issues"]


def test_journal_request_mismatch_blocks_binding(tmp_path):
    result = run_fixture(tmp_path, request_mismatch=True)
    assert result["status"] == "BLOCKED"
    assert "request_binding" in result["issues"]


def test_uncertain_incomplete_journal_is_preserved_and_counted(tmp_path):
    manifest, report, results = make_fixture(tmp_path)
    ledger_path = results / "spending" / "development_pilot.jsonl"
    ledger_rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    ledger_rows[-1] = {
        "kind": "uncertain",
        "schema_version": "engraphis-campaign-ledger/1",
        "call_id": "attempt-test-reader-0",
        "call_kind": "reader",
        "error_class": "interrupted_before_completion",
    }
    ledger_path.write_text(
        "".join(canonical_json(item) + "\n" for item in ledger_rows), encoding="utf-8"
    )
    journal_path = results / "oauth-transport" / "attempt-test" / "events.jsonl"
    journal_rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    journal_path.write_text(
        "".join(canonical_json(item) + "\n" for item in journal_rows[:4]), encoding="utf-8"
    )
    result = audit_run(
        manifest_path=manifest,
        report_path=report,
        results=results,
        private_inventory=tmp_path / "private-inventory.json",
        output=tmp_path / "public-audit.json",
    )
    assert result["status"] == "BLOCKED"
    assert result["counts"]["uncertain_calls"] == 1
    assert result["counts"]["calls_without_usage"] == 1
    assert result["counts"]["native_turns_incomplete"] == 1
    assert "noncomplete_call" in result["issues"]
    assert "native_turn_incomplete" in result["issues"]


def test_ledger_response_hash_tamper_blocks(tmp_path):
    manifest, report, results = make_fixture(tmp_path)
    ledger_path = results / "spending" / "development_pilot.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    rows[-1]["response"] = '{"answer":"tampered","citations":[]}'
    ledger_path.write_text(
        "".join(canonical_json(item) + "\n" for item in rows), encoding="utf-8"
    )
    result = audit_run(
        manifest_path=manifest,
        report_path=report,
        results=results,
        private_inventory=tmp_path / "private-inventory.json",
        output=tmp_path / "public-audit.json",
    )
    assert result["status"] == "BLOCKED"
    assert "ledger_response_hash" in result["issues"]


def test_checkpoint_private_response_tamper_blocks(tmp_path):
    manifest, report, results = make_fixture(tmp_path)
    checkpoint_path = next((results / "development_pilot").glob("*.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["row"]["private_responses"][0]["answer"] = "tampered"
    checkpoint["row_sha256"] = digest(checkpoint["row"])
    checkpoint_path.write_text(canonical_json(checkpoint) + "\n", encoding="utf-8")
    result = audit_run(
        manifest_path=manifest,
        report_path=report,
        results=results,
        private_inventory=tmp_path / "private-inventory.json",
        output=tmp_path / "public-audit.json",
    )
    assert result["status"] == "BLOCKED"
    assert "checkpoint_response_binding" in result["issues"]


def test_duplicate_native_usage_blocks(tmp_path):
    manifest, report, results = make_fixture(tmp_path)
    journal_path = results / "oauth-transport" / "attempt-test" / "events.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    usage = next(item for item in rows if item.get("event") == "usage")
    rows.insert(-1, dict(usage))
    journal_path.write_text(
        "".join(canonical_json(item) + "\n" for item in rows), encoding="utf-8"
    )
    result = audit_run(
        manifest_path=manifest,
        report_path=report,
        results=results,
        private_inventory=tmp_path / "private-inventory.json",
        output=tmp_path / "public-audit.json",
    )
    assert result["status"] == "BLOCKED"
    assert "journal_usage_duplicate" in result["issues"]
